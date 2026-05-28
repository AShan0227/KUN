"""Mission Director runner for KUN Control Plane.

The Mission Director is the product-facing supervision role that sits above
individual workers.  It does not replace Qi, Nuo, or the daemon; it reviews
whether the mission is still aligned with the user's objective, whether the
task plan is complete enough, whether decomposition/worker assignment is
healthy, and whether delivery claims are backed by real acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.rainflow_ad_mission import RAINFLOW_AD_PRODUCTION_MODE
from kun.control_plane.runtime import InMemoryControlPlane, WorkItemResult
from kun.control_plane.v6 import (
    ArtifactManifest,
    ArtifactRecord,
    ExecutionContract,
    GateEvaluation,
    Mission,
    MissionStatus,
    TaskPlan,
    WorkItem,
)
from kun.interface.llm import ModelTier

MISSION_DIRECTOR_OWNER = "mission-director"


class MissionDirectorModelConfig(BaseModel):
    """Configurable model identity for the Mission Director role.

    The current runner is deterministic by default so it can run inside daemon
    tests and offline installations.  The configured model is still recorded on
    every review artifact so deployments can route the role to a dedicated LLM
    when an LLM-backed implementation is attached.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(
        default_factory=lambda: os.getenv("KUN_MISSION_DIRECTOR_MODEL_ID", "gpt-5.5")
    )
    model_tier: ModelTier = Field(
        default_factory=lambda: _model_tier_from_env("KUN_MISSION_DIRECTOR_MODEL_TIER", "top")
    )
    provider: str = Field(
        default_factory=lambda: os.getenv("KUN_MISSION_DIRECTOR_PROVIDER", "configured")
    )
    mode: Literal["deterministic", "llm_assisted"] = Field(
        default_factory=lambda: (
            os.getenv("KUN_MISSION_DIRECTOR_MODE", "deterministic")  # type: ignore[arg-type]
            if os.getenv("KUN_MISSION_DIRECTOR_MODE", "deterministic")
            in {"deterministic", "llm_assisted"}
            else "deterministic"
        )
    )


class MissionDirectorRunner:
    """Supervise mission progress against the north-star delivery standard."""

    runner_type: Literal["agent"] = "agent"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        model_config: MissionDirectorModelConfig | None = None,
    ) -> None:
        self.control_plane = control_plane
        self.model_config = model_config or MissionDirectorModelConfig()

    @property
    def runner_identity(self) -> str:
        return f"mission-director:{_slug(self.model_config.model_id)}"

    def can_run(self, work_item: WorkItem) -> bool:
        return work_item.owner == MISSION_DIRECTOR_OWNER and work_item.type in {
            "review",
            "governance",
            "plan_change",
        }

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="Mission Director only handles mission-director review/governance work.",
                failure_category="tool_failure",
            )
        mission = self.control_plane.missions.get(work_item.mission_id)
        if mission is None:
            return WorkItemResult(
                status="failed",
                summary=f"Mission Director could not find mission {work_item.mission_id}.",
                failure_category="plan_failure",
            )
        plan = _current_plan(self.control_plane, mission)
        contract = _current_contract(self.control_plane, mission)
        payload = _build_director_payload(
            control_plane=self.control_plane,
            mission=mission,
            plan=plan,
            contract=contract,
            work_item=work_item,
            model_config=self.model_config,
        )
        artifact = _artifact(
            work_item=work_item,
            payload=payload,
            model_config=self.model_config,
        )
        gate = _gate_from_payload(
            mission=mission,
            work_item=work_item,
            payload=payload,
            artifact=artifact,
        )
        followups = _followups_from_gate(work_item=work_item, gate=gate)

        # V7 Phase X.B.MF-1: emit V7 §9.7 MissionAlignmentReview alongside the
        # V6 payload so the new mission_alignment_reviews table真接到生产链路.
        # Fire-and-forget; bridge handles its own error path / opt-out env var.
        # See kun.integration.mission_director_v7_bridge for design rationale
        # (closes attacker-audit P0 finding "production path 不必经").
        try:
            from kun.integration.mission_director_v7_bridge import (
                emit_v7_review_for_work_item_sync,
            )

            emit_v7_review_for_work_item_sync(
                control_plane=self.control_plane,
                mission=mission,
                work_item=work_item,
                payload=payload,
            )
        except Exception:
            # Defense in depth: V6 path must not break if bridge import / hook fails
            pass

        return WorkItemResult(
            status="done",
            summary=str(payload["summary"]),
            artifacts=[artifact],
            gate_evaluation=gate,
            followup_work_items=followups,
        )


def _model_tier_from_env(env_key: str, default: ModelTier) -> ModelTier:
    value = os.getenv(env_key, default)
    if value in {"top", "strong", "coding", "cheap", "fallback"}:
        return value  # type: ignore[return-value]
    return default


def _build_director_payload(
    *,
    control_plane: InMemoryControlPlane,
    mission: Mission,
    plan: TaskPlan | None,
    contract: ExecutionContract | None,
    work_item: WorkItem,
    model_config: MissionDirectorModelConfig,
) -> dict[str, object]:
    work_items = [
        item for item in control_plane.work_items.values() if item.mission_id == mission.mission_id
    ]
    status_counts = Counter(item.status for item in work_items)
    owner_counts = Counter(item.owner for item in work_items)
    open_tickets = [
        ticket
        for ticket in control_plane.collaboration_tickets.values()
        if ticket.mission_id == mission.mission_id
        and ticket.status in {"open", "waiting", "escalated", "fallback_selected"}
    ]
    delivery_manifest_refs = [
        ref
        for ref in mission.artifact_manifest_refs
        if (manifest := control_plane.artifact_manifests.get(ref)) is not None
        and manifest.kind == "delivery"
        and manifest.supports_delivery
    ]
    open_current_plan_work = _has_open_current_plan_user_work(
        work_items=work_items,
        task_plan_version=plan.version if plan is not None else mission.current_plan_version,
    )
    latest_gate = _latest_gate(control_plane, mission)
    supports = {
        support
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission.mission_id
        for support in artifact.supports
    }
    manifests = [
        manifest
        for manifest in control_plane.artifact_manifests.values()
        if manifest.mission_id == mission.mission_id
    ]
    final_product_contract = _final_product_contract(contract)
    findings: list[str] = []
    if plan is None:
        findings.append("task_plan_missing")
    else:
        if plan.info_gaps:
            findings.append("task_plan_info_gaps_unresolved")
        if not plan.acceptance_criteria:
            findings.append("acceptance_criteria_missing")
        if not plan.decomposition:
            findings.append("decomposition_missing")
        if not plan.worker_plan:
            findings.append("worker_distribution_missing")
        if not plan.test_plan:
            findings.append("test_plan_missing")
        if plan.approval_status not in {"approved", "approved_with_limits"}:
            findings.append("task_plan_not_approved")
    if (
        final_product_contract
        and delivery_manifest_refs
        and mission.acceptance_ref is None
        and not open_current_plan_work
    ):
        findings.append("human_or_target_user_acceptance_missing")
    if (
        final_product_contract
        and mission.status in {"delivering", "awaiting_acceptance"}
        and not open_current_plan_work
        and not supports.intersection(
            {
                "browser_visual_screenshot",
                "browser_interaction_replay",
                "human_playtest",
                "target_user_playtest",
                "user_acceptance",
                "player_first_impression_gate",
                "player_perception_evidence",
            }
        )
    ):
        findings.append("player_perception_evidence_missing")
    if (
        mission.task_type == "product_development"
        and latest_gate is not None
        and latest_gate.next_action in {"ready_to_deliver", "accepted"}
        and mission.acceptance_ref is None
        and not open_current_plan_work
    ):
        findings.append("gate_pass_not_product_done")
    if mission.status in {"blocked", "repairing"} and not any(
        item.status == "queued" for item in work_items
    ):
        findings.append("blocked_without_ready_recovery_work")
    findings.extend(
        _rainflow_findings(
            mission=mission,
            contract=contract,
            work_item=work_item,
            work_items=work_items,
            artifacts=[
                artifact
                for artifact in control_plane.artifacts.values()
                if artifact.mission_id == mission.mission_id
            ],
            manifests=manifests,
            gates=[
                gate
                for gate in control_plane.gate_evaluations.values()
                if gate.mission_id == mission.mission_id
            ],
        )
    )
    findings = list(dict.fromkeys(findings))

    severity = _severity(findings)
    decision = _director_decision(mission=mission, findings=findings)
    return {
        "schema": "kun-v6-mission-director-review-v1",
        "mission_id": mission.mission_id,
        "work_item_id": work_item.work_item_id,
        "model_config": model_config.model_dump(mode="json"),
        "north_star": "delivery result quality first; speed and cost cannot offset incomplete results",
        "mission_status": mission.status,
        "task_type": mission.task_type,
        "current_plan_version": mission.current_plan_version,
        "plan_review": {
            "exists": plan is not None,
            "approval_status": plan.approval_status if plan is not None else None,
            "info_gap_count": len(plan.info_gaps) if plan is not None else None,
            "acceptance_criteria_count": len(plan.acceptance_criteria)
            if plan is not None
            else None,
            "decomposition_count": len(plan.decomposition) if plan is not None else None,
            "worker_plan_count": len(plan.worker_plan) if plan is not None else None,
            "test_plan_count": len(plan.test_plan) if plan is not None else None,
        },
        "execution_review": {
            "work_item_status_counts": dict(status_counts),
            "work_item_owner_counts": dict(owner_counts),
            "open_ticket_count": len(open_tickets),
            "delivery_manifest_refs": delivery_manifest_refs,
            "latest_gate_ref": latest_gate.gate_evaluation_id if latest_gate else None,
            "latest_gate_action": latest_gate.next_action if latest_gate else None,
        },
        "findings": findings,
        "decision": decision,
        "severity": severity,
        "summary": _summary(findings=findings, decision=decision),
        "required_next_actions": _required_next_actions(findings),
    }


def _gate_from_payload(
    *,
    mission: Mission,
    work_item: WorkItem,
    payload: dict[str, object],
    artifact: ArtifactRecord,
) -> GateEvaluation | None:
    decision = str(payload["decision"])
    if decision == "monitor_continue":
        if mission.status not in {"queued", "running", "planning", "contracted"}:
            return None
        return _gate(
            mission=mission,
            work_item=work_item,
            artifact=artifact,
            verdict="pass",
            result_quality=0.9,
            next_action="continue",
            next_state="running",
            hard_gate_failures=[],
            root_cause="mission director found no blocking alignment issue",
        )
    if decision == "request_information":
        return _gate(
            mission=mission,
            work_item=work_item,
            artifact=artifact,
            verdict="partial",
            result_quality=0.62,
            next_action="needs_info",
            next_state="info_gap",
            hard_gate_failures=list(payload.get("findings", [])),
            root_cause="mission director found unresolved information or plan gaps",
        )
    if decision == "request_human_playtest":
        return _gate(
            mission=mission,
            work_item=work_item,
            artifact=artifact,
            verdict="partial",
            result_quality=0.7,
            next_action="needs_human",
            next_state="waiting_human",
            hard_gate_failures=list(payload.get("findings", [])),
            root_cause="mission director requires human or target-user product acceptance",
        )
    return _gate(
        mission=mission,
        work_item=work_item,
        artifact=artifact,
        verdict="partial",
        result_quality=0.66,
        next_action="needs_plan_change",
        next_state="changing_plan",
        hard_gate_failures=list(payload.get("findings", [])),
        root_cause="mission director found that gate/test pass is not enough for final delivery",
    )


def _gate(
    *,
    mission: Mission,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    verdict: Literal["pass", "partial", "fail"],
    result_quality: float,
    next_action: Literal["continue", "needs_info", "needs_human", "needs_plan_change"],
    next_state: MissionStatus,
    hard_gate_failures: list[str],
    root_cause: str,
) -> GateEvaluation:
    return GateEvaluation(
        gate_evaluation_id=f"gate-mission-director-{_slug(work_item.work_item_id)}",
        mission_id=mission.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="acceptance" if mission.task_type == "product_development" else "plan",
        task_type=mission.task_type,
        rubric_version="kun-v6-mission-director-v1",
        metric_pack_version="kun-v6-mission-director-v1",
        north_star_verdict=verdict,
        result_quality=result_quality,
        speed=0.72,
        cost=0.82,
        risk=0.25 if verdict == "pass" else 0.58,
        evidence_quality=0.84,
        collaboration_quality=0.82,
        score_breakdown={"mission_director_alignment": result_quality},
        thresholds={"result_quality": 0.8},
        hard_gate_failures=hard_gate_failures,
        evidence_refs=[artifact.artifact_id],
        artifact_refs=[artifact.artifact_id],
        source_freshness="fresh",
        root_cause=root_cause,
        responsibility_scope="kun_auto",
        confidence=0.86,
        next_action=next_action,
        next_state=next_state,
        governance_signal="mission_director_supervision",
        created_by=MISSION_DIRECTOR_OWNER,
    )


def _followups_from_gate(
    *,
    work_item: WorkItem,
    gate: GateEvaluation | None,
) -> list[WorkItem]:
    if gate is None or gate.next_action != "needs_plan_change":
        return []
    if gate.task_type != "product_development":
        return []
    return [
        WorkItem(
            work_item_id=f"work-qi-strategy-replay-{_slug(work_item.work_item_id)}",
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="research",
            owner="qi",
            priority=min(100, work_item.priority + 4),
            dependencies=[work_item.work_item_id],
            idempotency_key=f"mission-director-strategy-replay:{work_item.work_item_id}",
            expected_output=(
                "Run a Qi-owned strategy replay and process audit for this product residual. "
                "Compare the old execution path with a stricter player-experience strategy, "
                "record evidence gaps and acceptance deltas, and keep all learning in replay "
                "or governance evidence only."
            ),
            recovery_refs=[gate.gate_evaluation_id, work_item.work_item_id],
        ),
        WorkItem(
            work_item_id=f"work-kun-plan-change-{_slug(work_item.work_item_id)}",
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="research",
            owner="kun",
            priority=min(100, work_item.priority + 5),
            dependencies=[work_item.work_item_id],
            idempotency_key=f"mission-director-plan-change:{work_item.work_item_id}",
            expected_output=(
                "Revise the product task plan from the Mission Director blocker. Add concrete "
                "player-facing acceptance criteria, assign the next implementation/test slice, "
                "and require fresh browser/playtest evidence before delivery can close."
            ),
            recovery_refs=[gate.gate_evaluation_id, work_item.work_item_id],
        ),
    ]


def _current_plan(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> TaskPlan | None:
    if mission.current_plan_version is None:
        return None
    candidates = [
        plan
        for plan in control_plane.task_plans.values()
        if plan.mission_id == mission.mission_id and plan.version == mission.current_plan_version
    ]
    return candidates[-1] if candidates else None


def _current_contract(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> ExecutionContract | None:
    if mission.execution_contract_ref:
        contract = control_plane.contracts.get(mission.execution_contract_ref)
        if contract is not None:
            return contract
    for contract in control_plane.contracts.values():
        if contract.mission_id == mission.mission_id:
            return contract
    return None


def _latest_gate(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> GateEvaluation | None:
    gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission.mission_id
    ]
    return gates[-1] if gates else None


def _final_product_contract(contract: ExecutionContract | None) -> bool:
    if contract is None:
        return False
    delivery = contract.delivery_contract
    return bool(
        delivery.get("final_player_experience_required")
        or delivery.get("human_acceptance_required")
        or delivery.get("human_playtest_required")
        or delivery.get("production_mode")
        in {"scribble_adventure_functional_parity_v1", RAINFLOW_AD_PRODUCTION_MODE}
    )


def _rainflow_findings(
    *,
    mission: Mission,
    contract: ExecutionContract | None,
    work_item: WorkItem,
    work_items: list[WorkItem],
    artifacts: list[ArtifactRecord],
    manifests: list[ArtifactManifest],
    gates: list[GateEvaluation],
) -> list[str]:
    if not _is_rainflow_context(mission=mission, contract=contract):
        return []
    findings: list[str] = []
    relevant_artifacts = _rainflow_relevant_artifacts(
        artifacts=artifacts,
        task_plan_version=work_item.task_plan_version,
    )
    supports = {support for artifact in relevant_artifacts for support in artifact.supports}
    delivery_manifests = [manifest for manifest in manifests if manifest.kind == "delivery"]
    required_supports = set(
        contract.evidence_policy.get("required", [])
        if contract is not None and isinstance(contract.evidence_policy.get("required", []), list)
        else []
    )
    if not _has_rainflow_gap_audit(
        supports=supports,
        artifacts=relevant_artifacts,
    ):
        findings.append("rainflow_gap_audit_missing")
    if "demo_comparison" in required_supports and "demo_comparison" not in supports:
        findings.append("rainflow_demo_comparison_missing")
    if "rollback_evidence" in required_supports and not _has_rainflow_rollback_evidence(
        supports=supports,
        manifests=delivery_manifests,
    ):
        findings.append("rainflow_rollback_evidence_missing")
    phase1_passed = _phase1_gate_passed(
        gates,
        task_plan_version=work_item.task_plan_version,
    ) or _phase1_acceptance_artifact_passed(relevant_artifacts)
    if _is_phase1_acceptance_review(work_item) and not phase1_passed:
        findings.append("rainflow_phase1_human_acceptance_missing")
    if _ai_stage_started(work_items) and not phase1_passed:
        findings.append("rainflow_phase1_gate_missing_before_ai_stage")
    return findings


def _is_phase1_acceptance_review(work_item: WorkItem) -> bool:
    text = f"{work_item.work_item_id}\n{work_item.expected_output}".lower()
    return "phase1-acceptance-review" in text


def _is_rainflow_contract(contract: ExecutionContract | None) -> bool:
    if contract is None:
        return False
    return contract.delivery_contract.get("production_mode") == RAINFLOW_AD_PRODUCTION_MODE


def _is_rainflow_context(*, mission: Mission, contract: ExecutionContract | None) -> bool:
    if _is_rainflow_contract(contract):
        return True
    payloads: list[object] = []
    if contract is not None:
        payloads.extend(
            [contract.delivery_contract, contract.risk_policy, contract.rollback_policy]
        )
    text = " ".join(
        [
            mission.mission_id,
            mission.objective,
            mission.current_plan_version or "",
            *[json.dumps(payload, sort_keys=True, ensure_ascii=False) for payload in payloads],
        ]
    ).lower()
    return any(
        token in text
        for token in (
            "rainflow",
            "information-flow ad",
            "information_flow_ad",
            "adflow",
            "phase1_mixed_edit",
        )
    )


def _rainflow_relevant_artifacts(
    *,
    artifacts: list[ArtifactRecord],
    task_plan_version: str | None,
) -> list[ArtifactRecord]:
    if not task_plan_version:
        return artifacts
    token = task_plan_version.lower()
    relevant = [
        artifact
        for artifact in artifacts
        if token
        in " ".join(
            [
                artifact.artifact_id,
                artifact.path_or_uri,
                artifact.work_item_id or "",
                *artifact.supports,
            ]
        ).lower()
    ]
    return relevant or artifacts


def _has_rainflow_gap_audit(
    *,
    supports: set[str],
    artifacts: list[ArtifactRecord],
) -> bool:
    if supports.intersection(
        {"engineering_gap_report", "code_vs_doc_gap_audit", "implementation_gap_audit"}
    ):
        return True
    for artifact in artifacts:
        path = artifact.path_or_uri.lower()
        if any(
            token in path
            for token in (
                "gap-audit",
                "gap_audit",
                "code-vs-doc",
                "code_vs_doc",
                "engineering-gap",
                "engineering_gap",
                "implementation-gap",
                "implementation_gap",
                "workspace_diff_and_artifact_inventory",
            )
        ):
            return True
        text = _read_text_artifact(artifact)
        if text and _rainflow_report_closes_gap(text):
            return True
    return False


def _rainflow_report_closes_gap(text: str) -> bool:
    normalized = text.lower()
    if (
        "baseline-vs-current comparison" in normalized
        and "accepted all required repairs" in normalized
    ):
        return True
    if (
        "workspace_diff_and_artifact_inventory" in normalized
        and "rollback / isolation" in normalized
    ):
        return True
    return (
        "stronger hook" in normalized and "conversion cta" in normalized and "pacing" in normalized
    )


def _has_rainflow_rollback_evidence(
    *,
    supports: set[str],
    manifests: list[ArtifactManifest],
) -> bool:
    if supports.intersection(
        {"rollback_evidence", "rollback_plan", "runtime_delivery_rollback_plan"}
    ):
        return True
    return any(manifest.rollback_refs for manifest in manifests)


def _ai_stage_started(work_items: list[WorkItem]) -> bool:
    done_work_items = {item.work_item_id for item in work_items if item.status == "done"}
    for item in work_items:
        if item.status == "cancelled":
            continue
        if not any(
            token in item.work_item_id
            for token in (
                "stage1-transition",
                "stage2-regeneration",
                "stage3-advanced-creative",
            )
        ):
            continue
        if item.status == "queued":
            # Planned downstream AI stages are allowed as long as they remain
            # dependency-blocked behind the Phase 1 acceptance gate.
            if set(item.dependencies).issubset(done_work_items):
                return True
            continue
        return True
    return False


def _phase1_gate_passed(
    gates: list[GateEvaluation],
    *,
    task_plan_version: str | None = None,
) -> bool:
    relevant = [
        gate
        for gate in gates
        if (task_plan_version is None or gate.task_plan_version == task_plan_version)
        and _is_phase1_gate(gate)
    ]
    blockers = [
        gate
        for gate in relevant
        if gate.north_star_verdict != "pass"
        or gate.hard_gate_failures
        or gate.next_action
        in {
            "needs_info",
            "needs_human",
            "needs_repair",
            "needs_rollback",
            "needs_plan_change",
            "rejected",
        }
    ]
    passes = [
        gate
        for gate in relevant
        if gate.north_star_verdict == "pass"
        and not gate.hard_gate_failures
        and gate.result_quality >= gate.thresholds.get("result_quality", 0.8)
    ]
    return bool(passes) and not blockers


def _phase1_acceptance_artifact_passed(artifacts: list[ArtifactRecord]) -> bool:
    for artifact in artifacts:
        payloads = _phase1_acceptance_payloads(artifact)
        for payload in payloads:
            if _phase1_acceptance_payload_is_pass(payload):
                return True
    return False


def _phase1_acceptance_payloads(artifact: ArtifactRecord) -> list[dict[str, object]]:
    paths = _phase1_acceptance_candidate_paths(artifact)
    payloads: list[dict[str, object]] = []
    for path in paths:
        try:
            if path.exists() and path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    payloads.append(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return payloads


def _phase1_acceptance_candidate_paths(artifact: ArtifactRecord) -> list[Path]:
    if "://" in artifact.path_or_uri:
        return []
    path = Path(artifact.path_or_uri)
    paths = [path]
    if path.name == "final_execution_report.md":
        paths.append(path.parent / "phase1_acceptance_summary.json")
    return paths


def _phase1_acceptance_payload_is_pass(payload: dict[str, object]) -> bool:
    if payload.get("human_accepted") is True:
        if payload.get("phase1_gate_status") not in {None, "ready", "passed", "pass"}:
            return False
        blockers = payload.get("phase1_gate_blockers")
        if isinstance(blockers, list) and blockers:
            return False
        return payload.get("baseline_comparison_status") in {None, "accepted", "pass", "passed"}
    if payload.get("accepted_for_phase1") is True and payload.get("decision") in {
        "accept_phase1_demo",
        "accepted",
        "pass",
    }:
        blockers = payload.get("blockers")
        if isinstance(blockers, list) and blockers:
            return False
        audit = payload.get("concrete_phase1_structure_audit")
        if isinstance(audit, dict):
            audit_blockers = audit.get("blockers")
            if isinstance(audit_blockers, list) and audit_blockers:
                return False
        checks = payload.get("checks")
        if isinstance(checks, dict):
            required = {
                "strong_hook",
                "clear_structure",
                "conversion_path",
                "creator_spoken_sales_safe",
                "smooth_pacing",
                "render_review_ready",
            }
            if not all(checks.get(key) is True for key in required):
                return False
        return True
    review = payload.get("review")
    if payload.get("status") == "ready" and isinstance(review, dict):
        blockers = payload.get("blockers")
        if isinstance(blockers, list) and blockers:
            return False
        result_approval = payload.get("closed_loop_review")
        approval_state = ""
        if isinstance(result_approval, dict):
            supervisor = result_approval.get("result_approval")
            if isinstance(supervisor, dict):
                approval_state = str(supervisor.get("state") or "")
        return approval_state in {"approve_for_small_spend_ab", "accepted", "pass"}
    return False


def _is_phase1_gate(gate: GateEvaluation) -> bool:
    return any(
        token in gate.subject_ref or token in gate.gate_evaluation_id
        for token in ("phase1-acceptance-review", "phase1_acceptance")
    )


def _has_open_current_plan_user_work(
    *,
    work_items: list[WorkItem],
    task_plan_version: str | None,
) -> bool:
    if task_plan_version is None:
        return False
    return any(
        item.task_plan_version == task_plan_version
        and item.owner != MISSION_DIRECTOR_OWNER
        and "-acceptance-rework-" not in item.work_item_id
        and item.status in {"queued", "running", "partial"}
        for item in work_items
    )


def _director_decision(*, mission: Mission, findings: list[str]) -> str:
    if not findings:
        return "monitor_continue"
    if any(
        finding in findings for finding in {"task_plan_missing", "task_plan_info_gaps_unresolved"}
    ) and mission.status in {"planning", "queued", "running", "contracted", "info_gap"}:
        return "request_information"
    if set(findings).issubset(
        {
            "human_or_target_user_acceptance_missing",
            "rainflow_phase1_human_acceptance_missing",
        }
    ):
        return "request_human_playtest"
    return "force_plan_change"


def _severity(findings: list[str]) -> str:
    if any(
        finding
        in {
            "gate_pass_not_product_done",
            "player_perception_evidence_missing",
            "human_or_target_user_acceptance_missing",
            "rainflow_demo_comparison_missing",
            "rainflow_rollback_evidence_missing",
            "rainflow_phase1_human_acceptance_missing",
            "rainflow_phase1_gate_missing_before_ai_stage",
        }
        for finding in findings
    ):
        return "high"
    if findings:
        return "medium"
    return "low"


def _summary(*, findings: list[str], decision: str) -> str:
    if not findings:
        return "Mission Director found the mission aligned enough to continue."
    return (
        "Mission Director blocked premature completion: "
        f"{', '.join(findings)}. Decision={decision}."
    )


def _required_next_actions(findings: list[str]) -> list[str]:
    actions: list[str] = []
    if "task_plan_missing" in findings or "task_plan_info_gaps_unresolved" in findings:
        actions.append("补齐任务信息和任务方案后再执行。")
    if "decomposition_missing" in findings or "worker_distribution_missing" in findings:
        actions.append("补齐拆解、worker 分配、合并和复测路径。")
    if "gate_pass_not_product_done" in findings:
        actions.append("把门禁通过降级为证据，继续按最终交付标准压任务。")
    if "player_perception_evidence_missing" in findings:
        actions.append("补真实浏览器视觉/交互回放、人工试玩或目标用户试玩证据。")
    if "human_or_target_user_acceptance_missing" in findings:
        actions.append("请求人类或目标用户验收；未验收前不得关闭产品任务。")
    if "rainflow_gap_audit_missing" in findings:
        actions.append("先补 RainFlow 已实现能力与文档承诺的代码差距审计，再继续声称阶段完成。")
    if "rainflow_demo_comparison_missing" in findings:
        actions.append("补基线对比 demo 证据，证明 hook、结构、节奏和 CTA 真实变强。")
    if "rainflow_rollback_evidence_missing" in findings:
        actions.append("补可回滚证据或回滚清单；没有回滚路径不得推进 RainFlow 阶段交付。")
    if "rainflow_phase1_human_acceptance_missing" in findings:
        actions.append("Phase 1 必须有人类或目标用户明确验收通过；内部报告不能自己放行 AI 阶段。")
    if "rainflow_phase1_gate_missing_before_ai_stage" in findings:
        actions.append("先让 Phase 1 mixed-edit 通过，再开启任何 RainFlow AI 视频生成阶段。")
    return actions or ["继续执行并保持观察。"]


def _artifact(
    *,
    work_item: WorkItem,
    payload: dict[str, object],
    model_config: MissionDirectorModelConfig,
) -> ArtifactRecord:
    payload_hash = _hash_payload(payload)
    return ArtifactRecord(
        artifact_id=f"artifact-mission-director-{_slug(work_item.work_item_id)}-{payload_hash[:12]}",
        kind="review",
        path_or_uri=(
            "control-plane://mission-director/"
            f"{work_item.mission_id}/{work_item.work_item_id}/{payload_hash[:12]}"
        ),
        content_hash=payload_hash,
        created_by=MISSION_DIRECTOR_OWNER,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[
            "mission_director_review",
            "north_star_supervision",
            "task_decomposition_review",
            "worker_distribution_review",
            "delivery_not_gate_only_guard",
            f"mission_director_model:{_slug(model_config.model_id)}",
            f"mission_director_tier:{model_config.model_tier}",
        ],
        freshness="fresh",
        source_quality="primary",
    )


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _read_text_artifact(artifact: ArtifactRecord) -> str:
    if "://" in artifact.path_or_uri:
        return ""
    path = Path(artifact.path_or_uri)
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size > 256_000:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    slug = "".join(safe).strip("-") or "item"
    if len(slug) <= 80:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:67].rstrip('-')}-{digest}"


__all__ = [
    "MISSION_DIRECTOR_OWNER",
    "MissionDirectorModelConfig",
    "MissionDirectorRunner",
]
