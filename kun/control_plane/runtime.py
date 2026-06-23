"""Minimal executable V6 control-plane runtime.

This module turns the V6 product contract into a deterministic runtime surface:
missions can be submitted, queued work can run through a supervisor loop, every
state movement is ledgered, and gates decide the next mission state.  Durable DB,
process isolation, and external AB wiring can sit behind this interface later.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_governance import (
    CapabilityGovernanceReport,
    govern_default_runtime_capabilities,
)
from kun.control_plane.collaboration import CollaborationResponse
from kun.control_plane.nuo import (
    NuoHealthReport,
    NuoObservation,
    build_nuo_recovery_plan,
    diagnose_nuo_health,
)
from kun.control_plane.store import ControlPlaneStore
from kun.control_plane.v6 import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    FailureCategory,
    GateEvaluation,
    LedgerEvent,
    LedgerEventType,
    Mission,
    MissionStatus,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemStatus,
    assert_transition_allowed,
    default_recovery_for_failure,
    validate_workitem_dag,
)

# Local copy of the rainflow production-mode discriminator. Duplicated here so
# the core runtime does not reverse-depend on a domain module. If the domain
# renames its mode string, update both copies (or migrate to a shared
# kun/control_plane/production_modes.py).
RAINFLOW_AD_PRODUCTION_MODE = "rainflow_information_flow_ad_v1"

if TYPE_CHECKING:
    from kun.control_plane.capability_evolution import (
        CapabilityPromotion,
        CapabilityPromotionStage,
        CapabilityRollback,
    )

RunnerType = Literal["model", "tool", "agent", "command", "human", "external_worker"]
RunExitStatus = Literal["running", "succeeded", "failed", "cancelled"]


def _now() -> datetime:
    return datetime.now(UTC)


def _failure_still_blocks_progress(
    status: MissionStatus,
    latest_gate: GateEvaluation | None,
) -> bool:
    if latest_gate and latest_gate.north_star_verdict == "pass":
        return False
    return status in {
        "blocked",
        "retrying",
        "repairing",
        "rolling_back",
        "changing_plan",
        "failed",
    }


def _diagnose_result_with_nuo(
    *,
    mission: Mission,
    work_item: WorkItem,
    result: WorkItemResult,
    manifest_ref: str | None,
    contract: ExecutionContract | None,
) -> NuoHealthReport | None:
    if not _should_route_to_nuo(work_item=work_item, result=result):
        return None
    text = _result_text(result)
    failed_text = _failure_text_for_nuo(result) or _environment_blocker_text_for_nuo(text)
    observation = NuoObservation(
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        task_type=mission.task_type,
        output_text=text,
        error_text=failed_text,
        fallback_engaged=_mentions_fallback_pollution(text),
        timed_out=(
            bool(failed_text)
            and ("timeout" in failed_text.lower() or "timed out" in failed_text.lower())
        ),
        network_eof=_network_transport_interrupted(text),
        wrapper_missing="wrapper missing" in text.lower() or "wrapper not found" in text.lower(),
        auth_failure="unauthorized" in text.lower() or "invalid api key" in text.lower(),
        report_required=_runtime_report_required(
            work_item=work_item, result=result, contract=contract
        ),
        report_ref=_runtime_report_ref(result=result, manifest_ref=manifest_ref),
        review_count=_runtime_review_count(result),
        expected_review_count=_expected_review_count(contract),
        product_acceptance_claimed=_product_acceptance_claimed(
            result=result,
            manifest_ref=manifest_ref,
        ),
        product_surface_gap_codes=_product_surface_gap_codes(
            result=result,
            text=text,
            contract=contract,
        ),
        human_playtest_required=_runtime_human_playtest_required(
            mission=mission,
            work_item=work_item,
            result=result,
            contract=contract,
        ),
        human_playtest_ref=_runtime_human_playtest_ref(
            result=result,
            manifest_ref=manifest_ref,
            work_item=work_item,
        ),
        artifact_refs=[artifact.artifact_id for artifact in result.artifacts],
        evidence_refs=_runtime_manifest_refs(result, "evidence_refs"),
        test_refs=_runtime_manifest_refs(result, "test_refs"),
        review_refs=_runtime_manifest_refs(result, "review_refs"),
        rollback_refs=_runtime_rollback_refs(result=result, work_item=work_item),
    )
    return diagnose_nuo_health(observation)


def _lease_allows_ready(
    work_item: WorkItem,
    *,
    lease: str | None,
    now: datetime,
) -> bool:
    if not work_item.lease:
        return True
    if lease is not None and work_item.lease == lease:
        return True
    return work_item.timeout is not None and work_item.timeout <= now


def _should_route_to_nuo(*, work_item: WorkItem, result: WorkItemResult) -> bool:
    if result.artifact_manifest is not None and result.artifact_manifest.kind == "delivery":
        return True
    if (
        result.gate_evaluation is not None
        and result.gate_evaluation.next_action == "ready_to_deliver"
    ):
        return True
    if result.failure_category is not None:
        return True
    if result.status in {"failed", "blocked", "cancelled", "waiting_external"}:
        return True
    text = _route_trigger_text_for_nuo(result).lower()
    if any(
        token in text
        for token in (
            "stub echo",
            "fallback model",
            "fallback path",
            "fallback engaged",
            "timeout",
            "timed out",
            "unexpected eof",
            "network eof",
            "tls handshake eof",
            "stream disconnected",
            "failed to connect to websocket",
            "http/request failed",
            "dns error",
            "connection reset",
            "operation not permitted",
            "sc_sem_nsems_max",
            "processpoolexecutor",
            "unauthorized",
            "permission denied",
            "wrapper missing",
            "schema mismatch",
            "report missing",
            "review missing",
            "mechanics only",
            "mechanism only",
            "visual missing",
            "art missing",
            "ui missing",
            "playtest missing",
            "human playtest required",
            "subjective acceptance required",
            "机制雏形",
            "只有机制",
            "图片不足",
            "角色不足",
            "真实试玩不足",
            "需要人工试玩",
            "需要用户验收",
        )
    ):
        return True
    return _runtime_report_required(work_item=work_item, result=result, contract=None)


def _mentions_fallback_pollution(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "using fallback model",
            "fallback model path",
            "fallback model",
            "fallback path engaged",
            "fallback engaged",
        )
    )


def _result_text(result: WorkItemResult) -> str:
    parts = [result.summary, result.failure_category or ""]
    for artifact in result.artifacts:
        parts.extend([artifact.kind, artifact.path_or_uri, " ".join(artifact.supports)])
    return "\n".join(part for part in parts if part)


def _route_trigger_text_for_nuo(result: WorkItemResult) -> str:
    """Only route clean work to Nuo for explicit output claims, not evidence labels."""

    return "\n".join(part for part in [result.summary, result.failure_category or ""] if part)


def _failure_text_for_nuo(result: WorkItemResult) -> str:
    if result.status not in {"failed", "blocked", "cancelled"}:
        return ""
    return "\n".join(part for part in [result.summary, result.failure_category or ""] if part)


def _environment_blocker_text_for_nuo(text: str) -> str:
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "operation not permitted",
            "sc_sem_nsems_max",
            "processpoolexecutor",
            "permission denied",
            "access denied",
            "forbidden",
        )
    ):
        return text
    return ""


def _network_transport_interrupted(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "unexpected eof",
            "network eof",
            "tls handshake eof",
            "stream disconnected",
            "connection reset by peer",
            "failed to connect to websocket",
            "http/request failed",
            "dns error",
        )
    )


def _runtime_report_required(
    *,
    work_item: WorkItem,
    result: WorkItemResult,
    contract: ExecutionContract | None,
) -> bool:
    if result.status not in {"done", "partial"}:
        return False
    policy = contract.delivery_contract if contract is not None else {}
    if isinstance(policy, dict):
        if bool(policy.get("report_required")):
            return True
        if bool(policy.get("requires_report")):
            return True
    text = f"{work_item.type}\n{work_item.expected_output}".lower()
    return any(token in text for token in ("report", "summary", "验收", "报告"))


def _runtime_report_ref(*, result: WorkItemResult, manifest_ref: str | None) -> str | None:
    if manifest_ref:
        return manifest_ref
    for artifact in result.artifacts:
        if artifact.kind == "report" or any("report" in support for support in artifact.supports):
            return artifact.artifact_id
    return None


def _runtime_review_count(result: WorkItemResult) -> int | None:
    if result.artifact_manifest is not None and result.artifact_manifest.review_refs:
        return len(result.artifact_manifest.review_refs)
    reviews = [
        artifact
        for artifact in result.artifacts
        if artifact.kind == "review" or "review" in artifact.supports
    ]
    return len(reviews) if reviews else None


def _expected_review_count(contract: ExecutionContract | None) -> int:
    if contract is None:
        return 0
    candidates = [
        contract.delivery_contract,
        contract.evidence_policy,
        contract.risk_policy,
    ]
    keys = ("expected_review_count", "minimum_review_count", "review_count_required")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 0


def _runtime_manifest_refs(result: WorkItemResult, field_name: str) -> list[str]:
    if result.artifact_manifest is None:
        return []
    value = getattr(result.artifact_manifest, field_name)
    return list(value)


def _runtime_rollback_refs(*, result: WorkItemResult, work_item: WorkItem) -> list[str]:
    refs = [*work_item.rollback_refs, *_runtime_manifest_refs(result, "rollback_refs")]
    refs.extend(
        artifact.artifact_id
        for artifact in result.artifacts
        if artifact.kind == "rollback"
        or artifact.kind == "checkpoint"
        or any("rollback" in support or "checkpoint" in support for support in artifact.supports)
    )
    return _dedupe_refs(refs)


def _product_acceptance_claimed(
    *,
    result: WorkItemResult,
    manifest_ref: str | None,
) -> bool:
    text = _result_text(result).lower()
    if result.artifact_manifest is not None and result.artifact_manifest.kind == "delivery":
        return True
    if manifest_ref:
        return True
    if result.status == "blocked" and any(
        token in text
        for token in (
            "final delivery blocked",
            "delivery blocked",
            "交付被阻断",
            "交付阻断",
        )
    ):
        return False
    return any(
        token in text
        for token in (
            "ready_to_deliver",
            "ready to deliver",
            "ready for delivery",
            "final delivery",
            "final_delivery",
            "delivery_manifest",
            "supports_delivery",
            "delivery ready",
            "client-deliverable approved",
            "acceptance package",
            "验收通过",
            "交付就绪",
            "可以交付",
            "可交付",
            "完成交付",
        )
    )


def _product_surface_gap_codes(
    *,
    result: WorkItemResult,
    text: str,
    contract: ExecutionContract | None,
) -> list[str]:
    lowered = text.lower()
    gap_markers = {
        "mechanics_only": ("mechanics only", "mechanism only", "机制雏形", "只有机制"),
        "visual_gap": (
            "visual missing",
            "art missing",
            "ui missing",
            "visual product iteration evidence",
            "visual iteration artifact",
            "no real visual iteration artifact",
            "图片不足",
            "角色不足",
            "美术不足",
        ),
        "interaction_gap": ("interaction gap", "交互不足", "没有游戏性质"),
        "experience_gap": ("not fun", "unfun", "体验不足", "不好玩"),
        "playtest_gap": ("playtest missing", "试玩缺失", "真实试玩不足"),
    }
    codes = [
        code
        for code, markers in gap_markers.items()
        if any(marker in lowered for marker in markers)
    ]
    supports = {support for artifact in result.artifacts for support in artifact.supports}
    if (
        "directly_playable_game" in supports
        and _contract_requires_visual_product_evidence(contract)
        and "visual_product_iteration" not in supports
        and "visual_gap" not in codes
    ):
        codes.append("visual_gap")
    if (
        result.status == "blocked"
        and "final delivery blocked by mission director" in lowered
        and (
            "action=needs_plan_change" in lowered
            or "needs_plan_change" in lowered
            or "continue_iteration" in lowered
            or "continue iteration" in lowered
        )
        and "mission_director_plan_change" not in codes
    ):
        codes.append("mission_director_plan_change")
    return codes


def _contract_requires_visual_product_evidence(contract: ExecutionContract | None) -> bool:
    if contract is None or not isinstance(contract.delivery_contract, dict):
        return False
    return bool(contract.delivery_contract.get("visual_product_iteration_required", False))


def _runtime_human_playtest_required(
    *,
    mission: Mission,
    work_item: WorkItem,
    result: WorkItemResult,
    contract: ExecutionContract | None,
) -> bool:
    if mission.task_type != "product_development":
        return False
    if work_item.owner in {"qi", "nuo"} or work_item.type == "governance":
        return False
    policy = contract.delivery_contract if contract is not None else {}
    if isinstance(policy, dict) and bool(
        policy.get("human_playtest_required") or policy.get("human_acceptance_required")
    ):
        manifest_ref = (
            result.artifact_manifest.manifest_id if result.artifact_manifest is not None else None
        )
        if _product_acceptance_claimed(result=result, manifest_ref=manifest_ref) or (
            _work_item_targets_product_acceptance(work_item)
            and result.status in {"done", "partial", "blocked"}
        ):
            return True
    text = f"{work_item.expected_output}\n{_result_text(result)}".lower()
    explicit_human_playtest_request = any(
        token in text
        for token in (
            "action=needs_human",
            "human playtest required",
            "target-user playtest required",
            "subjective acceptance required",
            "human/player-experience evidence",
            "human player-experience evidence",
            "需要人工试玩",
            "需要用户验收",
        )
    )
    if not explicit_human_playtest_request:
        return False
    manifest_ref = (
        result.artifact_manifest.manifest_id if result.artifact_manifest is not None else None
    )
    return _product_acceptance_claimed(
        result=result,
        manifest_ref=manifest_ref,
    ) or _work_item_targets_product_acceptance(work_item)


def _work_item_targets_product_acceptance(work_item: WorkItem) -> bool:
    text = f"{work_item.work_item_id}\n{work_item.type}\n{work_item.phase or ''}\n{work_item.expected_output}".lower()
    phase = (work_item.phase or "").lower().strip()
    if work_item.type == "test" and phase in {
        "internal-test",
        "build-test",
        "browser-test",
        "visual-test",
        "regression-gates",
        "fun-and-browser-retest",
        "player-experience-retest",
    }:
        return False
    return any(
        token in text
        for token in (
            "final_delivery",
            "final delivery",
            "delivery_gate",
            "delivery gate",
            "human_acceptance",
            "human acceptance",
            "human-simulated",
            "user acceptance",
            "target-user",
            "target user",
            "target_user",
            "final_player",
            "final player",
            "human_playtest",
            "human playtest",
            "phase1_acceptance",
            "acceptance review",
            "验收",
            "人工试玩",
        )
    )


def _runtime_human_playtest_ref(
    *,
    result: WorkItemResult,
    manifest_ref: str | None,
    work_item: WorkItem | None = None,
) -> str | None:
    for artifact in result.artifacts:
        supports = set(artifact.supports)
        if supports.intersection(
            {
                "human_playtest",
                "target_user_playtest",
                "user_acceptance",
                "acceptance_review",
            }
        ):
            return artifact.artifact_id
    if result.artifact_manifest is not None:
        for ref in [*result.artifact_manifest.review_refs, *result.artifact_manifest.evidence_refs]:
            lowered = ref.lower()
            if "human" in lowered or "acceptance" in lowered or "playtest" in lowered:
                return ref
    if manifest_ref and ("acceptance" in manifest_ref.lower() or "human" in manifest_ref.lower()):
        return manifest_ref
    if work_item is not None:
        for ref in [
            *work_item.recovery_refs,
            *work_item.external_source_refs,
            *work_item.checkpoint_refs,
        ]:
            lowered = ref.lower()
            if (
                "human" in lowered
                or "playtest" in lowered
                or "target-user" in lowered
                or "target_user" in lowered
                or "user_acceptance" in lowered
            ):
                return ref
    return None


def _nuo_report_artifact(
    *,
    mission_id: str,
    work_item_id: str,
    report: NuoHealthReport,
) -> ArtifactRecord:
    payload = report.model_dump(mode="json")
    return ArtifactRecord(
        artifact_id=f"artifact-nuo-runtime-health-{_slug(work_item_id)}",
        kind="report",
        path_or_uri=f"control-plane://nuo/runtime-health/{mission_id}/{work_item_id}",
        content_hash=_hash_payload(payload),
        created_by="nuo",
        mission_id=mission_id,
        work_item_id=work_item_id,
        supports=[
            "nuo_runtime_health",
            f"nuo_status:{report.status}",
            *[f"nuo_finding:{finding.code}" for finding in report.findings],
        ],
        freshness="fresh",
        source_quality="primary",
    )


def _runtime_qi_learning_work_item(
    *,
    mission: Mission,
    work_item: WorkItem,
    result: WorkItemResult,
    gate: GateEvaluation | None,
    nuo_report: NuoHealthReport | None,
) -> WorkItem | None:
    if work_item.owner in {"qi", "nuo"}:
        return None
    if work_item.owner == "external-supervisor-gpt5.5":
        return None
    if _runtime_signal_is_mission_director_supervision(work_item=work_item, gate=gate):
        return None
    if _runtime_signal_is_collaboration_only(work_item=work_item, gate=gate):
        return None
    signals: list[str] = []
    if result.failure_category is not None:
        signals.append(f"failure:{result.failure_category}")
    if result.status in {"failed", "blocked", "cancelled", "waiting_external"}:
        signals.append(f"status:{result.status}")
    if gate is not None and gate.north_star_verdict != "pass":
        signals.append(f"gate:{gate.governance_signal or gate.north_star_verdict}")
    if nuo_report is not None:
        if nuo_report.findings:
            signals.extend(f"nuo:{finding.code}" for finding in nuo_report.findings)
        elif result.failure_category is not None or result.status in {"failed", "blocked"}:
            signals.append("nuo:no_known_pollution_pattern")
    if not signals:
        return None
    unique_signals = sorted(set(signals))
    return WorkItem(
        work_item_id=f"work-qi-runtime-learning-{_slug(work_item.work_item_id)}",
        mission_id=mission.mission_id,
        task_plan_version=work_item.task_plan_version,
        type="governance",
        owner="qi",
        dependencies=[],
        priority=80,
        idempotency_key=f"qi-runtime-learning:{work_item.work_item_id}:{','.join(unique_signals)}",
        expected_output=(
            "Review this runtime signal and decide whether KUN should keep, merge, "
            "modify, delete, or promote a capability. If useful, create a capability "
            "evolution candidate with evidence, holdout/shadow/canary gates, rollback "
            f"plan, and production eligibility. Signals: {', '.join(unique_signals)}"
        ),
        required_capability_refs=list(work_item.required_capability_refs),
        external_source_refs=list(work_item.external_source_refs),
        recovery_refs=[
            *([gate.gate_evaluation_id] if gate is not None else []),
            work_item.work_item_id,
        ],
    )


def _runtime_signal_is_mission_director_supervision(
    *,
    work_item: WorkItem,
    gate: GateEvaluation | None,
) -> bool:
    """Mission Director emits its own targeted Qi/KUN follow-ups.

    Re-wrapping the same supervision gate as generic runtime-learning creates
    duplicate governance work after product runs complete, which can keep long
    daemons busy without producing product change.
    """

    return (
        work_item.owner == "mission-director"
        and gate is not None
        and gate.governance_signal == "mission_director_supervision"
    )


def _runtime_signal_is_collaboration_only(
    *,
    work_item: WorkItem,
    gate: GateEvaluation | None,
) -> bool:
    """Avoid turning pure human-acceptance supervision into Qi churn."""

    if work_item.owner != "mission-director" or gate is None:
        return False
    if gate.next_action != "needs_human":
        return False
    collaboration_failures = {
        "human_or_target_user_acceptance_missing",
        "human_acceptance_missing",
        "player_perception_evidence_missing",
        "target_user_playtest_missing",
    }
    failures = set(gate.hard_gate_failures)
    return not failures or failures.issubset(collaboration_failures)


def _runtime_validation_gate(
    *,
    mission: Mission,
    work_item: WorkItem,
    result: WorkItemResult,
    manifest_ref: str | None,
    artifact_refs: list[str],
) -> GateEvaluation | None:
    """Apply the default V6 quality gate when a runner omits its own gate."""

    if result.status not in {"done", "partial"}:
        return None
    trace_refs = _dedupe_refs([*artifact_refs, *([manifest_ref] if manifest_ref else [])])
    if not (result.summary or trace_refs):
        return GateEvaluation(
            gate_evaluation_id=f"gate-validation-{_slug(work_item.work_item_id)}",
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            subject_ref=work_item.work_item_id,
            stage="workitem",
            task_type=mission.task_type,
            rubric_version="kun-v6-runtime-validation-v1",
            metric_pack_version="north-star-v6",
            north_star_verdict="fail",
            result_quality=0.0,
            speed=0.5,
            cost=0.5,
            risk=0.7,
            evidence_quality=0.0,
            collaboration_quality=0.5,
            thresholds={"result_quality": 0.8},
            hard_gate_failures=["missing_summary_and_artifact_trace"],
            artifact_refs=trace_refs,
            failure_category="delivery_failure",
            root_cause="Runner marked work complete without summary, artifacts, or manifest.",
            responsibility_scope="kun_auto",
            confidence=0.86,
            next_action="needs_repair",
            next_state="repairing",
            learning_eligibility="candidate",
            governance_signal="missing_runtime_delivery_trace",
            created_by="validation-pipeline",
        )

    is_partial = result.status == "partial"
    quality = 0.68 if is_partial else 0.82
    verdict: Literal["pass", "partial", "fail"] = "partial" if is_partial else "pass"
    return GateEvaluation(
        gate_evaluation_id=f"gate-validation-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type=mission.task_type,
        rubric_version="kun-v6-runtime-validation-v1",
        metric_pack_version="north-star-v6",
        north_star_verdict=verdict,
        result_quality=quality,
        speed=0.7,
        cost=0.7,
        risk=0.25 if not is_partial else 0.45,
        evidence_quality=0.75 if trace_refs else 0.55,
        collaboration_quality=0.7,
        thresholds={"result_quality": 0.8},
        artifact_refs=trace_refs,
        evidence_refs=trace_refs,
        confidence=0.72 if trace_refs else 0.62,
        next_action="continue",
        next_state="running",
        created_by="validation-pipeline",
    )


def _coerce_gate_for_current_mission_state(
    *,
    mission: Mission,
    gate: GateEvaluation,
) -> GateEvaluation:
    """Keep stricter plan-change state when concurrent follow-ups race.

    Qi and Nuo follow-up work can be prepared from the same `running` mission and
    then finish in either order. If a Qi governance result already advanced the
    mission to `changing_plan`, a later Nuo repair gate must not try to force the
    mission back into `repairing`, which is both a less strict outcome and an
    invalid transition in the state graph.
    """

    if mission.status == "changing_plan" and gate.next_state == "repairing":
        return gate.model_copy(
            update={
                "next_action": "needs_plan_change",
                "next_state": "changing_plan",
            }
        )
    return gate


def _dedupe_refs(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _ticket_matches_clean_retest_blocker(ticket: CollaborationTicket, work_item: WorkItem) -> bool:
    if ticket.type not in {"operator_action", "approval", "external_action", "user_decision"}:
        return False
    if work_item.work_item_id in ticket.auto_resolvable_by:
        return True
    explicit_refs = {
        work_item.work_item_id,
        *work_item.recovery_refs,
        *work_item.checkpoint_refs,
        *work_item.external_source_refs,
    }
    explicit_refs = {ref for ref in explicit_refs if ref}
    if set(ticket.resolution_refs).intersection(explicit_refs):
        return True
    if ticket.context_ref in explicit_refs or ticket.ticket_id in explicit_refs:
        return True
    idempotency_key = work_item.idempotency_key or ""
    return ticket.ticket_id in idempotency_key or ticket.context_ref in idempotency_key


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _is_rainflow_contract(contract: ExecutionContract | None) -> bool:
    if contract is None or not isinstance(contract.delivery_contract, dict):
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


def _rainflow_required_gate_tokens(work_item_id: str) -> tuple[str, ...]:
    if "stage1-transition" in work_item_id:
        return ("phase1-acceptance-review",)
    if "stage2-regeneration" in work_item_id:
        return ("stage1-retest-and-gate", "stage1-acceptance-review")
    if "stage3-advanced-creative" in work_item_id:
        return ("stage2-retest-and-gate", "stage2-acceptance-review")
    return ()


def _rainflow_stage_gate_passes_cleanly(
    *,
    gates: Sequence[GateEvaluation],
    required_gate_tokens: Sequence[str],
) -> bool:
    latest_by_subject: dict[str, GateEvaluation] = {}
    for gate in gates:
        if any(
            token in gate.subject_ref or token in gate.gate_evaluation_id
            for token in required_gate_tokens
        ):
            latest_by_subject[gate.subject_ref] = gate
    relevant = list(latest_by_subject.values())
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


def _slug(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-")
    if not safe:
        return "item"
    if len(safe) <= 80:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:67].rstrip('-_')}-{digest}"


def _acceptance_decision_from_collaboration_response(
    response: CollaborationResponse,
) -> Literal["accepted", "partial_accepted", "rework_required", "rejected"] | None:
    selected = (response.selected_option or "").strip().lower()
    answer = response.answer.strip().lower()
    if selected in {"accepted", "accept", "approve", "approved"}:
        return "accepted"
    if selected in {"partial_accepted", "partial", "accept_with_caveats"}:
        return "partial_accepted"
    if selected in {"rework_required", "rework", "iterate", "request_rework"}:
        return "rework_required"
    if selected in {"rejected", "reject"}:
        return "rejected"
    if not answer:
        return None
    if any(token in answer for token in ("rework required", "needs rework", "request rework")):
        return "rework_required"
    if any(token in answer for token in ("reject", "rejected")):
        return "rejected"
    if any(token in answer for token in ("partial accept", "partially accepted")):
        return "partial_accepted"
    # Negation guard (audit F013): "do not accept", "not approved", "unacceptable"
    # etc. all contain the substring "accept"/"approve" and were misclassified as
    # accepted by the affirmative check below — wrongly closing the task. An
    # explicitly negated acceptance is sent back for rework (never accepted).
    if any(
        token in answer
        for token in (
            "not accept",
            "don't accept",
            "do not accept",
            "cannot accept",
            "can't accept",
            "won't accept",
            "will not accept",
            "not accepted",
            "unacceptable",
            "not acceptable",
            "not approve",
            "don't approve",
            "do not approve",
            "cannot approve",
            "can't approve",
            "not approved",
            "disapprove",
        )
    ):
        return "rework_required"
    if any(token in answer for token in ("accept", "accepted", "approve", "approved")):
        return "accepted"
    return None


def _acceptance_satisfaction_for_decision(
    decision: Literal["accepted", "partial_accepted", "rework_required", "rejected"],
) -> float:
    if decision == "accepted":
        return 0.9
    if decision == "partial_accepted":
        return 0.65
    if decision == "rework_required":
        return 0.4
    return 0.2


def _latest_delivery_gate_for_manifest(
    gates: Iterable[GateEvaluation],
    *,
    mission_id: str,
    manifest_ref: str,
) -> GateEvaluation | None:
    matches = [
        gate
        for gate in gates
        if gate.mission_id == mission_id
        and gate.stage == "delivery"
        and gate.subject_ref == manifest_ref
    ]
    if not matches:
        return None
    return max(matches, key=lambda gate: gate.gate_evaluation_id)


class WorkItemResult(BaseModel):
    """Normalized runner output accepted by the V6 control plane."""

    model_config = ConfigDict(extra="forbid")

    status: WorkItemStatus
    summary: str = ""
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    artifact_manifest: ArtifactManifest | None = None
    gate_evaluation: GateEvaluation | None = None
    failure_category: FailureCategory | None = None
    collaboration_tickets: list[CollaborationTicket] = Field(default_factory=list)
    followup_work_items: list[WorkItem] = Field(default_factory=list)


class ControlPlaneRunner(Protocol):
    """Runner boundary for model/tool/agent/process workers."""

    @property
    def runner_type(self) -> RunnerType:
        """Kind of runner behind this boundary."""
        ...

    @property
    def runner_identity(self) -> str:
        """Stable identity recorded into RunRecord."""
        ...

    def run(self, work_item: WorkItem) -> WorkItemResult:
        """Execute one work item and return normalized output."""


class ControlPlaneProgressReport(BaseModel):
    """Operator-facing summary for long-running mission progress."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    status: MissionStatus
    current_plan_version: str | None
    total_work_items: int
    work_item_counts: dict[str, int]
    open_collaboration_ticket_ids: list[str] = Field(default_factory=list)
    latest_gate_ref: str | None = None
    latest_gate_action: str | None = None
    latest_gate_verdict: str | None = None
    latest_failure_category: FailureCategory | None = None
    next_ready_work_item_ids: list[str] = Field(default_factory=list)
    ledger_event_count: int = 0
    artifact_manifest_count: int = 0


class InMemoryControlPlane:
    """Strict V6 runtime protocol with optional store-backed recovery."""

    def __init__(self, *, store: ControlPlaneStore | None = None) -> None:
        self.store = store
        self.missions: dict[str, Mission] = {}
        self.task_plans: dict[str, TaskPlan] = {}
        self.contracts: dict[str, ExecutionContract] = {}
        self.working_contexts: dict[str, WorkingContext] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.runs: dict[str, RunRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.artifact_manifests: dict[str, ArtifactManifest] = {}
        self.ledger_events: dict[str, LedgerEvent] = {}
        self.collaboration_tickets: dict[str, CollaborationTicket] = {}
        self.gate_evaluations: dict[str, GateEvaluation] = {}
        self.acceptance_reviews: dict[str, AcceptanceReview] = {}
        self.capability_profiles: dict[str, CapabilityProfile] = {}
        self._ledger_sequences: Counter[str] = Counter()
        self._lock = RLock()
        if self.store is not None:
            self._hydrate_from_store(self.store)

    def refresh_from_store(self) -> None:
        """Reload all control-plane records so long-running daemons see external writes."""

        if self.store is None:
            return
        reload_store = getattr(self.store, "reload", None)
        with self._lock:
            if callable(reload_store):
                reload_store()
            self._hydrate_from_store(self.store)

    def submit_mission(
        self,
        *,
        mission: Mission,
        task_plan: TaskPlan,
        execution_contract: ExecutionContract,
        working_context: WorkingContext,
        work_items: list[WorkItem],
        actor: str = "kun",
    ) -> Mission:
        """Register a fully contracted mission and queue its work items."""

        if mission.mission_id != task_plan.mission_id:
            raise ValueError("mission and task_plan mission_id mismatch")
        if mission.mission_id != execution_contract.mission_id:
            raise ValueError("mission and execution_contract mission_id mismatch")
        if mission.mission_id != working_context.mission_id:
            raise ValueError("mission and working_context mission_id mismatch")
        if execution_contract.task_plan_version != task_plan.version:
            raise ValueError("execution_contract task_plan_version mismatch")
        if working_context.task_plan_version != task_plan.version:
            raise ValueError("working_context task_plan_version mismatch")
        if mission.status != "contracted":
            raise ValueError("submit_mission requires a contracted mission")
        assert_transition_allowed(mission.status, "queued")
        if not task_plan.can_contract:
            raise ValueError("task_plan must be approved and have no info_gaps before execution")
        validate_workitem_dag(work_items)
        for item in work_items:
            if item.mission_id != mission.mission_id:
                raise ValueError(f"work item {item.work_item_id} mission_id mismatch")
            if item.task_plan_version != task_plan.version:
                raise ValueError(f"work item {item.work_item_id} task_plan_version mismatch")

        queued_mission = mission.model_copy(
            update={
                "status": "queued",
                "current_plan_version": task_plan.version,
                "execution_contract_ref": execution_contract.contract_id,
                "working_context_ref": working_context.working_context_id,
            }
        )
        self.missions[queued_mission.mission_id] = queued_mission
        self.task_plans[task_plan.plan_id] = task_plan
        self.contracts[execution_contract.contract_id] = execution_contract
        self.working_contexts[working_context.working_context_id] = working_context
        self.work_items.update({item.work_item_id: item for item in work_items})
        self._persist_task_plan(task_plan)
        self._persist_contract(execution_contract)
        self._persist_working_context(working_context)
        for item in work_items:
            self._persist_work_item(item)
        self._record_ledger_event(
            mission_id=queued_mission.mission_id,
            event_type="state_change",
            actor=actor,
            subject_ref=queued_mission.mission_id,
            before={"status": mission.status},
            after={"status": queued_mission.status},
            payload={"reason": "mission submitted with approved task plan"},
        )
        return self.missions[queued_mission.mission_id]

    def record_plan_change(
        self,
        *,
        mission_id: str,
        task_plan: TaskPlan,
        execution_contract: ExecutionContract,
        working_context: WorkingContext,
        work_items: list[WorkItem],
        actor: str,
        reason: str,
    ) -> Mission:
        """Record a new runnable task-plan version for an active mission.

        Plan changes are a first-class Control Plane operation: the new plan,
        contract, context, and follow-up work items are durably persisted and
        tied back to the mission ledger.  This is used when a long task learns
        that its original plan was incomplete or needs a research-first pass.
        """

        mission = self._mission(mission_id)
        if task_plan.mission_id != mission_id:
            raise ValueError("task_plan mission_id mismatch")
        if execution_contract.mission_id != mission_id:
            raise ValueError("execution_contract mission_id mismatch")
        if working_context.mission_id != mission_id:
            raise ValueError("working_context mission_id mismatch")
        if execution_contract.task_plan_version != task_plan.version:
            raise ValueError("execution_contract task_plan_version mismatch")
        if working_context.task_plan_version != task_plan.version:
            raise ValueError("working_context task_plan_version mismatch")
        if not task_plan.can_contract:
            raise ValueError("changed task_plan must be approved and have no info_gaps")
        for item in work_items:
            if item.mission_id != mission_id:
                raise ValueError(f"work item {item.work_item_id} mission_id mismatch")
            if item.task_plan_version != task_plan.version:
                raise ValueError(f"work item {item.work_item_id} task_plan_version mismatch")
            if item.work_item_id in self.work_items:
                raise ValueError(f"work item already exists: {item.work_item_id}")

        validate_workitem_dag(list(self._mission_work_items(mission_id)) + work_items)
        self.task_plans[task_plan.plan_id] = task_plan
        self.contracts[execution_contract.contract_id] = execution_contract
        self.working_contexts[working_context.working_context_id] = working_context
        self.work_items.update({item.work_item_id: item for item in work_items})
        self._persist_task_plan(task_plan)
        self._persist_contract(execution_contract)
        self._persist_working_context(working_context)
        for item in work_items:
            self._persist_work_item(item)

        update_payload = {
            "current_plan_version": task_plan.version,
            "execution_contract_ref": execution_contract.contract_id,
            "working_context_ref": working_context.working_context_id,
        }
        if mission.status in {"delivering", "awaiting_acceptance"}:
            update_payload["status"] = "changing_plan"
        elif mission.status in {"waiting_human", "waiting_external", "paused"}:
            update_payload["status"] = "queued"
        updated = mission.model_copy(update=update_payload)
        self.missions[mission_id] = updated
        self._persist_mission(updated)
        self._record_ledger_event(
            mission_id=mission_id,
            event_type="plan_change",
            actor=actor,
            subject_ref=task_plan.plan_id,
            before={
                "plan_version": mission.current_plan_version,
                "execution_contract_ref": mission.execution_contract_ref,
                "working_context_ref": mission.working_context_ref,
            },
            after={
                "plan_version": task_plan.version,
                "execution_contract_ref": execution_contract.contract_id,
                "working_context_ref": working_context.working_context_id,
                "status": updated.status,
                "queued_work_item_ids": [item.work_item_id for item in work_items],
            },
            payload={"reason": reason},
        )
        return self.missions[mission_id]

    def transition_mission(
        self,
        *,
        mission_id: str,
        target: MissionStatus,
        actor: str,
        reason: str,
        subject_ref: str | None = None,
    ) -> Mission:
        """Move a mission through the V6 state machine and record the move."""

        mission = self._mission(mission_id)
        assert_transition_allowed(mission.status, target)
        updated = mission.model_copy(update={"status": target})
        self.missions[mission_id] = updated
        self._persist_mission(updated)
        self._record_ledger_event(
            mission_id=mission_id,
            event_type="state_change",
            actor=actor,
            subject_ref=subject_ref or mission_id,
            before={"status": mission.status},
            after={"status": target},
            payload={"reason": reason},
        )
        return updated

    def next_ready_work_item(self, mission_id: str) -> WorkItem | None:
        """Return the highest-priority queued item whose dependencies are done."""

        ready = self._ready_work_items(mission_id)
        return ready[0] if ready else None

    def ready_work_items(
        self,
        mission_id: str,
        *,
        now: datetime | None = None,
    ) -> list[WorkItem]:
        """Return all ready work items in priority order for batch scheduling."""

        return list(self._ready_work_items(mission_id, now=now))

    def run_next_ready(
        self,
        *,
        mission_id: str,
        runner: ControlPlaneRunner,
        preserve_mission_status: bool = False,
    ) -> RunRecord | None:
        """Run one ready work item and apply its normalized result."""

        work_item = self.next_ready_work_item(mission_id)
        if work_item is None:
            return None
        return self.run_work_item(
            work_item_id=work_item.work_item_id,
            runner=runner,
            preserve_mission_status=preserve_mission_status,
        )

    def run_work_item(
        self,
        *,
        work_item_id: str,
        runner: ControlPlaneRunner,
        preserve_mission_status: bool = False,
        lease: str | None = None,
    ) -> RunRecord | None:
        """Run one specific ready work item and apply its normalized result."""

        started = self.start_work_item_run(
            work_item_id=work_item_id,
            runner=runner,
            preserve_mission_status=preserve_mission_status,
            lease=lease,
        )
        if started is None:
            return None
        run, running_item = started
        try:
            result = runner.run(running_item)
        except Exception:  # pragma: no cover - deterministic tests cover normalized failure.
            result = WorkItemResult(status="failed", failure_category="tool_failure")
        return self.finish_work_item_run(
            run_id=run.run_id,
            result=result,
            preserve_mission_status=preserve_mission_status,
        )

    def start_work_item_run(
        self,
        *,
        work_item_id: str,
        runner: ControlPlaneRunner,
        preserve_mission_status: bool = False,
        lease: str | None = None,
    ) -> tuple[RunRecord, WorkItem] | None:
        """Atomically mark a ready item running, then let callers execute outside the lock.

        This is the boundary that lets the daemon run multiple already-claimed
        work items in parallel while keeping mission/work-item state changes
        serialized and auditable.
        """

        with self._lock:
            work_item = self.work_items.get(work_item_id)
            if work_item is None:
                raise ValueError(f"unknown work item {work_item_id}")
            if work_item.status != "queued":
                return None
            if work_item not in self._ready_work_items(work_item.mission_id, lease=lease):
                return None
            mission = self._mission(work_item.mission_id)
            if not preserve_mission_status:
                if mission.status in {"repairing", "rolling_back", "changing_plan", "paused"}:
                    self.transition_mission(
                        mission_id=work_item.mission_id,
                        target="queued",
                        actor="control-plane",
                        reason=f"resume queued work item from {mission.status}",
                        subject_ref=work_item.work_item_id,
                    )
                    mission = self._mission(work_item.mission_id)
                if mission.status == "queued":
                    self.transition_mission(
                        mission_id=work_item.mission_id,
                        target="running",
                        actor="control-plane",
                        reason="ready work item acquired",
                        subject_ref=work_item.work_item_id,
                    )
                elif mission.status != "running":
                    assert_transition_allowed(mission.status, "running")
                    self.transition_mission(
                        mission_id=work_item.mission_id,
                        target="running",
                        actor="control-plane",
                        reason="resume ready work item",
                        subject_ref=work_item.work_item_id,
                    )

            started = _now()
            running_item = work_item.model_copy(
                update={
                    "status": "running",
                    "heartbeat": started,
                    "lease": lease or work_item.lease,
                }
            )
            self.work_items[work_item.work_item_id] = running_item
            store_transaction = getattr(self.store, "transaction", None)
            context = store_transaction() if callable(store_transaction) else nullcontext()
            with context:
                self._persist_work_item(running_item)
                run = RunRecord(
                    work_item_id=running_item.work_item_id,
                    runner_type=runner.runner_type,
                    runner_identity=runner.runner_identity,
                    started_at=started,
                )
                self.runs[run.run_id] = run
                self._persist_run(run)
                self._record_ledger_event(
                    mission_id=work_item.mission_id,
                    event_type="state_change",
                    actor="control-plane",
                    subject_ref=running_item.work_item_id,
                    before={"status": work_item.status},
                    after={"status": "running"},
                    payload={"run_id": run.run_id},
                )
            return run, running_item

    def finish_work_item_run(
        self,
        *,
        run_id: str,
        result: WorkItemResult,
        preserve_mission_status: bool = False,
    ) -> RunRecord:
        """Apply a runner result under the runtime lock."""

        with self._lock:
            store_transaction = getattr(self.store, "transaction", None)
            context = store_transaction() if callable(store_transaction) else nullcontext()
            with context:
                return self.apply_work_item_result(
                    run_id=run_id,
                    result=result,
                    preserve_mission_status=preserve_mission_status,
                )

    def apply_work_item_result(
        self,
        *,
        run_id: str,
        result: WorkItemResult,
        preserve_mission_status: bool = False,
    ) -> RunRecord:
        """Persist runner output, apply gate decisions, and update mission state."""

        run = self.runs[run_id]
        work_item = self.work_items[run.work_item_id]
        mission = self._mission(work_item.mission_id)
        for artifact in result.artifacts:
            self.artifacts[artifact.artifact_id] = artifact
            self._persist_artifact(artifact)
        manifest_ref = None
        if result.artifact_manifest is not None:
            manifest_ref = result.artifact_manifest.manifest_id
            self.artifact_manifests[manifest_ref] = result.artifact_manifest
            self._persist_artifact_manifest(result.artifact_manifest)
            manifest_refs = _dedupe_refs(
                [*mission.artifact_manifest_refs, result.artifact_manifest.manifest_id]
            )
            mission = mission.model_copy(update={"artifact_manifest_refs": manifest_refs})
            self.missions[mission.mission_id] = mission
            self._persist_mission(mission)
        for ticket in result.collaboration_tickets:
            self.collaboration_tickets[ticket.ticket_id] = ticket
            self._persist_collaboration_ticket(ticket)
        for followup in result.followup_work_items:
            if followup.mission_id != work_item.mission_id:
                raise ValueError(f"followup work item {followup.work_item_id} mission_id mismatch")
            existing_followup = self.work_items.get(followup.work_item_id)
            if existing_followup is not None:
                should_requeue_existing = (
                    existing_followup.status in {"failed", "blocked"}
                    and followup.status == "queued"
                    and followup.work_item_id == existing_followup.work_item_id
                )
                merged_followup = existing_followup.model_copy(
                    update={
                        "status": "queued" if should_requeue_existing else existing_followup.status,
                        "lease": None if should_requeue_existing else existing_followup.lease,
                        "heartbeat": None
                        if should_requeue_existing
                        else existing_followup.heartbeat,
                        "timeout": None if should_requeue_existing else existing_followup.timeout,
                        "priority": max(existing_followup.priority, followup.priority),
                        "expected_output": (
                            followup.expected_output
                            if existing_followup.status in {"queued", "retrying"}
                            or should_requeue_existing
                            else existing_followup.expected_output
                        ),
                        "dependencies": _dedupe_refs(
                            [*existing_followup.dependencies, *followup.dependencies]
                        ),
                        "required_capability_refs": _dedupe_refs(
                            [
                                *existing_followup.required_capability_refs,
                                *followup.required_capability_refs,
                            ]
                        ),
                        "skill_refs": _dedupe_refs(
                            [*existing_followup.skill_refs, *followup.skill_refs]
                        ),
                        "external_source_refs": _dedupe_refs(
                            [
                                *existing_followup.external_source_refs,
                                *followup.external_source_refs,
                            ]
                        ),
                        "recovery_refs": _dedupe_refs(
                            [*existing_followup.recovery_refs, *followup.recovery_refs]
                        ),
                        "checkpoint_refs": _dedupe_refs(
                            [*existing_followup.checkpoint_refs, *followup.checkpoint_refs]
                        ),
                        "rollback_refs": _dedupe_refs(
                            [*existing_followup.rollback_refs, *followup.rollback_refs]
                        ),
                        "resource_locks": _dedupe_refs(
                            [*existing_followup.resource_locks, *followup.resource_locks]
                        ),
                    }
                )
                self.work_items[merged_followup.work_item_id] = merged_followup
                self._persist_work_item(merged_followup)
                continue
            self.work_items[followup.work_item_id] = followup
            self._persist_work_item(followup)

        runtime_artifact_refs = [artifact.artifact_id for artifact in result.artifacts]
        runtime_gate = result.gate_evaluation
        runtime_failure_category = result.failure_category
        if runtime_gate is not None:
            if runtime_gate.failure_category:
                runtime_failure_category = runtime_gate.failure_category
            elif (
                runtime_gate.north_star_verdict != "pass"
                and not _runtime_signal_is_collaboration_only(
                    work_item=work_item,
                    gate=runtime_gate,
                )
            ):
                runtime_failure_category = runtime_failure_category or "delivery_failure"
        nuo_report = _diagnose_result_with_nuo(
            mission=mission,
            work_item=work_item,
            result=result,
            manifest_ref=manifest_ref,
            contract=self.contracts.get(mission.execution_contract_ref or ""),
        )
        if nuo_report is not None:
            nuo_artifact = _nuo_report_artifact(
                mission_id=work_item.mission_id,
                work_item_id=work_item.work_item_id,
                report=nuo_report,
            )
            self.artifacts[nuo_artifact.artifact_id] = nuo_artifact
            self._persist_artifact(nuo_artifact)
            runtime_artifact_refs.append(nuo_artifact.artifact_id)
            if nuo_report.findings:
                nuo_plan = build_nuo_recovery_plan(nuo_report, depends_on_subject=False)
                runtime_gate = nuo_plan.gate_evaluation
                runtime_gate = runtime_gate.model_copy(
                    update={
                        "artifact_refs": _dedupe_refs(
                            [
                                *(
                                    result.gate_evaluation.artifact_refs
                                    if result.gate_evaluation
                                    else []
                                ),
                                *runtime_gate.artifact_refs,
                                nuo_artifact.artifact_id,
                            ]
                        ),
                        "evidence_refs": _dedupe_refs(
                            [
                                *(
                                    result.gate_evaluation.evidence_refs
                                    if result.gate_evaluation
                                    else []
                                ),
                                *runtime_gate.evidence_refs,
                                nuo_artifact.artifact_id,
                            ]
                        ),
                        "test_refs": _dedupe_refs(
                            [
                                *(
                                    result.gate_evaluation.test_refs
                                    if result.gate_evaluation
                                    else []
                                ),
                                *runtime_gate.test_refs,
                            ]
                        ),
                        "review_refs": _dedupe_refs(
                            [
                                *(
                                    result.gate_evaluation.review_refs
                                    if result.gate_evaluation
                                    else []
                                ),
                                *runtime_gate.review_refs,
                            ]
                        ),
                    }
                )
                runtime_failure_category = runtime_gate.failure_category or runtime_failure_category
                if (
                    nuo_plan.recovery_work_item is not None
                    and nuo_plan.recovery_work_item.work_item_id not in self.work_items
                ):
                    self.work_items[nuo_plan.recovery_work_item.work_item_id] = (
                        nuo_plan.recovery_work_item
                    )
                    self._persist_work_item(nuo_plan.recovery_work_item)

        qi_followup = _runtime_qi_learning_work_item(
            mission=mission,
            work_item=work_item,
            result=result,
            gate=runtime_gate,
            nuo_report=nuo_report,
        )
        if qi_followup is not None and qi_followup.work_item_id not in self.work_items:
            self.work_items[qi_followup.work_item_id] = qi_followup
            self._persist_work_item(qi_followup)

        if runtime_gate is None:
            runtime_gate = _runtime_validation_gate(
                mission=mission,
                work_item=work_item,
                result=result,
                manifest_ref=manifest_ref,
                artifact_refs=runtime_artifact_refs,
            )
            if runtime_gate is not None and runtime_gate.failure_category:
                runtime_failure_category = runtime_gate.failure_category

        gate_ref = None
        if runtime_gate is not None:
            gate_ref = runtime_gate.gate_evaluation_id
            self.gate_evaluations[gate_ref] = runtime_gate
            self._persist_gate(runtime_gate)
            self._resolve_clean_retest_coordination(
                mission_id=work_item.mission_id,
                work_item=work_item,
                gate=runtime_gate,
                artifact_refs=runtime_artifact_refs,
            )

        gate_failed = runtime_failure_category is not None and (
            runtime_gate is None or runtime_gate.north_star_verdict != "pass"
        )
        exit_status: RunExitStatus = (
            "failed"
            if (gate_failed and result.status != "partial")
            else "succeeded"
            if result.status in {"done", "partial"}
            else "failed"
        )
        if result.status in {"waiting_human", "waiting_external", "blocked"}:
            exit_status = "cancelled"
        work_item_status = result.status
        if exit_status == "failed" and work_item_status == "done":
            work_item_status = "failed"
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(),
                "ended_at": _now(),
                "exit_status": exit_status,
                "failure_category": runtime_failure_category,
                "artifact_manifest_ref": manifest_ref,
                "gate_evaluation_ref": gate_ref,
            }
        )
        self.runs[run_id] = updated_run
        self._persist_run(updated_run)
        updated_item = work_item.model_copy(
            update={
                "status": work_item_status,
                "artifact_manifest_ref": manifest_ref or work_item.artifact_manifest_ref,
                "heartbeat": _now(),
                "lease": None,
                "timeout": None,
            }
        )
        self.work_items[work_item.work_item_id] = updated_item
        self._persist_work_item(updated_item)
        self._record_ledger_event(
            mission_id=work_item.mission_id,
            event_type="state_change",
            actor="control-plane",
            subject_ref=work_item.work_item_id,
            before={"status": work_item.status},
            after={"status": work_item_status},
            payload={"run_id": run_id, "summary": result.summary},
            artifact_refs=runtime_artifact_refs,
        )

        if preserve_mission_status:
            if runtime_gate is not None:
                current_mission = self._mission(work_item.mission_id)
                runtime_gate = _coerce_gate_for_current_mission_state(
                    mission=current_mission,
                    gate=runtime_gate,
                )
                current_status = current_mission.status
                if runtime_gate.next_state == "running" and current_status in {
                    "info_gap",
                    "waiting_human",
                    "waiting_external",
                }:
                    open_tickets = any(
                        ticket.mission_id == work_item.mission_id
                        and ticket.status in {"open", "waiting", "escalated"}
                        for ticket in self.collaboration_tickets.values()
                    )
                    if open_tickets:
                        return updated_run
                try:
                    assert_transition_allowed(current_status, runtime_gate.next_state)
                except ValueError:
                    return updated_run
                self.apply_gate(runtime_gate)
            return updated_run
        if runtime_gate is not None:
            runtime_gate = _coerce_gate_for_current_mission_state(
                mission=self._mission(work_item.mission_id),
                gate=runtime_gate,
            )
            self.apply_gate(runtime_gate)
        elif runtime_failure_category is not None:
            recovery = default_recovery_for_failure(runtime_failure_category)
            self.transition_mission(
                mission_id=work_item.mission_id,
                target=recovery.next_state,
                actor="control-plane",
                reason=f"recovered from {runtime_failure_category}",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "waiting_human":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="waiting_human",
                actor="control-plane",
                reason="work item requires human collaboration",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "waiting_external":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="waiting_external",
                actor="control-plane",
                reason="work item requires external dependency",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "blocked":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="blocked",
                actor="control-plane",
                reason="work item blocked without runnable fallback",
                subject_ref=work_item.work_item_id,
            )
        return updated_run

    def _resolve_clean_retest_coordination(
        self,
        *,
        mission_id: str,
        work_item: WorkItem,
        gate: GateEvaluation,
        artifact_refs: list[str],
    ) -> None:
        """Close stale human blocker tickets once Nuo proves the blocker is gone.

        This is the coordination boundary between Nuo diagnostics and the main
        execution queue: a clean retest must not remain a passive report.  When
        it proves that a workspace/permission blocker is cleared, KUN retires
        the stale operator ticket and lets the gate resume the mission.
        """

        if gate.created_by != "nuo-runtime-repair-runner":
            return
        if gate.governance_signal != "nuo_clean_retest_passed":
            return
        if gate.north_star_verdict != "pass":
            return
        retired: list[str] = []
        for ticket in list(self.collaboration_tickets.values()):
            if ticket.mission_id != mission_id:
                continue
            if ticket.status not in {"open", "waiting", "escalated", "fallback_selected"}:
                continue
            if not _ticket_matches_clean_retest_blocker(ticket, work_item):
                continue
            updated = ticket.model_copy(
                update={
                    "status": "closed",
                    "resolution_refs": _dedupe_refs(
                        [
                            *ticket.resolution_refs,
                            gate.gate_evaluation_id,
                            *artifact_refs,
                            work_item.work_item_id,
                        ]
                    ),
                }
            )
            self.collaboration_tickets[ticket.ticket_id] = updated
            self._persist_collaboration_ticket(updated)
            retired.append(ticket.ticket_id)
        if not retired:
            return
        self._record_ledger_event(
            mission_id=mission_id,
            event_type="governance",
            actor=gate.created_by,
            subject_ref=work_item.work_item_id,
            payload={
                "reason": "Nuo clean retest proved the blocker was stale; closed related human tickets.",
                "closed_collaboration_ticket_ids": retired,
                "gate_evaluation_id": gate.gate_evaluation_id,
            },
            artifact_refs=artifact_refs,
        )

    def record_collaboration_ticket(
        self,
        ticket: CollaborationTicket,
        *,
        actor: str = "control-plane",
    ) -> CollaborationTicket:
        """Persist one human/external collaboration request and ledger it."""

        self._mission(ticket.mission_id)
        if ticket.ticket_id in self.collaboration_tickets:
            raise ValueError(f"collaboration ticket already exists: {ticket.ticket_id}")
        self.collaboration_tickets[ticket.ticket_id] = ticket
        self._persist_collaboration_ticket(ticket)
        self._record_ledger_event(
            mission_id=ticket.mission_id,
            event_type="message",
            actor=actor,
            subject_ref=ticket.ticket_id,
            payload={
                "sender": actor,
                "receiver": ticket.role_needed,
                "intent": ticket.type,
                "requires_response": ticket.resume_after_response,
                "deadline": ticket.deadline.isoformat(),
                "resume_rule": "resume_after_response"
                if ticket.resume_after_response
                else "record_only",
                "why_needed": ticket.why_needed,
                "recommended_option": ticket.recommended_option or "",
            },
        )
        return ticket

    def list_collaboration_tickets(self, mission_id: str) -> list[CollaborationTicket]:
        """List mission-scoped collaboration tickets for UI and operators."""

        self._mission(mission_id)
        return sorted(
            (
                ticket
                for ticket in self.collaboration_tickets.values()
                if ticket.mission_id == mission_id
            ),
            key=lambda ticket: ticket.ticket_id,
        )

    def emit_collaboration_sla_reminders(
        self,
        mission_id: str,
        *,
        now: datetime | None = None,
        actor: str = "control-plane",
    ) -> list[LedgerEvent]:
        """Ledger due collaboration reminders without changing execution state."""

        self._mission(mission_id)
        observed_at = now or _now()
        emitted: list[LedgerEvent] = []
        for ticket in self.list_collaboration_tickets(mission_id):
            if ticket.status not in {"open", "waiting", "escalated"}:
                continue
            reminder_after_hours = ticket.sla_policy.get("reminder_after_hours")
            if not isinstance(reminder_after_hours, int | float) or reminder_after_hours <= 0:
                continue
            opened_event = self._collaboration_ticket_opened_event(ticket.ticket_id)
            if opened_event is None:
                continue
            reminder_due_at = opened_event.time + timedelta(hours=float(reminder_after_hours))
            if observed_at < reminder_due_at:
                continue
            if self._collaboration_reminder_already_sent(ticket.ticket_id):
                continue
            emitted.append(
                self._record_ledger_event(
                    mission_id=mission_id,
                    event_type="message",
                    actor=actor,
                    subject_ref=ticket.ticket_id,
                    payload={
                        "sender": actor,
                        "receiver": ticket.role_needed,
                        "intent": "collaboration_reminder",
                        "requires_response": ticket.resume_after_response,
                        "deadline": ticket.deadline.isoformat(),
                        "resume_rule": "resume_after_response"
                        if ticket.resume_after_response
                        else "record_only",
                        "why_needed": ticket.why_needed,
                        "recommended_option": ticket.recommended_option or "",
                        "reminder_after_hours": reminder_after_hours,
                        "original_message_at": opened_event.time.isoformat(),
                        "risk_if_skipped": ticket.risk_if_skipped,
                    },
                )
            )
        return emitted

    def get_collaboration_ticket(self, ticket_id: str) -> CollaborationTicket:
        """Return one collaboration ticket or raise a stable runtime error."""

        try:
            return self.collaboration_tickets[ticket_id]
        except KeyError as exc:
            raise ValueError(f"unknown collaboration ticket {ticket_id}") from exc

    def record_collaboration_response(
        self,
        response: CollaborationResponse,
        *,
        actor: str = "control-plane",
    ) -> CollaborationTicket:
        """Record a human/external answer and resume the mission when possible."""

        ticket = self.get_collaboration_ticket(response.ticket_id)
        if ticket.status in {"cancelled", "closed"}:
            raise ValueError(f"ticket {ticket.ticket_id} is already {ticket.status}")
        if (
            response.selected_option
            and ticket.decision_options
            and response.selected_option not in ticket.decision_options
        ):
            raise ValueError("selected_option is not in decision_options")
        if response.status == "answered" and not (response.answer or response.selected_option):
            raise ValueError("answered collaboration response needs answer or selected_option")

        updated = ticket.model_copy(update={"status": response.status})
        self.collaboration_tickets[ticket.ticket_id] = updated
        self._persist_collaboration_ticket(updated)
        self._record_ledger_event(
            mission_id=ticket.mission_id,
            event_type="message",
            actor=actor,
            subject_ref=ticket.ticket_id,
            payload={
                "sender": response.responder,
                "receiver": "kun",
                "intent": f"collaboration_{response.status}",
                "requires_response": False,
                "deadline": response.received_at.isoformat(),
                "resume_rule": "resume" if response.resume_allowed else "do_not_resume",
                "selected_option": response.selected_option or "",
                "answer": response.answer,
            },
        )
        self._complete_collaboration_work_items_for_response(
            ticket=updated,
            response=response,
            actor=actor,
        )
        self._record_acceptance_review_from_collaboration_response(
            ticket=updated,
            response=response,
            actor=actor,
        )
        if response.resume_allowed and ticket.resume_after_response:
            self._resume_after_collaboration(ticket.mission_id)
        return updated

    def _record_acceptance_review_from_collaboration_response(
        self,
        *,
        ticket: CollaborationTicket,
        response: CollaborationResponse,
        actor: str,
    ) -> None:
        """Turn an answered delivery-review ticket into durable acceptance state."""

        if response.status != "answered" or ticket.type != "review":
            return
        mission = self._mission(ticket.mission_id)
        if mission.acceptance_ref is not None or mission.status != "awaiting_acceptance":
            return
        manifest = self.artifact_manifests.get(ticket.context_ref)
        if manifest is None or manifest.kind != "delivery":
            return
        decision = _acceptance_decision_from_collaboration_response(response)
        if decision is None:
            return
        latest_delivery_gate = _latest_delivery_gate_for_manifest(
            self.gate_evaluations.values(),
            mission_id=ticket.mission_id,
            manifest_ref=manifest.manifest_id,
        )
        if latest_delivery_gate is None:
            return
        requested_changes = []
        if decision in {"rework_required", "rejected"}:
            requested_changes = [response.answer or "Human review requested product rework."]
        review = AcceptanceReview(
            acceptance_id=f"accept-{_slug(ticket.ticket_id)}",
            mission_id=ticket.mission_id,
            task_plan_version=mission.current_plan_version or manifest.work_item_id or "unknown",
            delivery_manifest_ref=manifest.manifest_id,
            gate_evaluation_ref=latest_delivery_gate.gate_evaluation_id,
            reviewer=response.responder or actor,
            decision=decision,
            satisfaction=_acceptance_satisfaction_for_decision(decision),
            reason=response.answer,
            requested_changes=requested_changes,
            new_info_or_constraints=[
                f"Recorded from collaboration ticket {ticket.ticket_id}.",
            ],
        )
        self.record_acceptance_review(review, actor=actor)

    def apply_gate(self, gate: GateEvaluation) -> Mission:
        """Apply a unified V6 gate to mission state."""

        mission = self._mission(gate.mission_id)
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        if mission.status != gate.next_state:
            mission = self.transition_mission(
                mission_id=gate.mission_id,
                target=gate.next_state,
                actor=gate.created_by,
                reason=f"gate {gate.stage}:{gate.next_action}",
                subject_ref=gate.subject_ref,
            )
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="gate_evaluation",
            actor=gate.created_by,
            subject_ref=gate.subject_ref,
            payload={
                "gate_evaluation_id": gate.gate_evaluation_id,
                "stage": gate.stage,
                "next_action": gate.next_action,
                "north_star_verdict": gate.north_star_verdict,
                "result_quality": gate.result_quality,
            },
            artifact_refs=gate.artifact_refs,
        )
        return mission

    def record_acceptance_review(self, review: AcceptanceReview, *, actor: str = "kun") -> None:
        """Persist user/operator acceptance and advance to the appropriate state."""

        mission = self._mission(review.mission_id)
        self.acceptance_reviews[review.acceptance_id] = review
        self._persist_acceptance_review(review)
        target: MissionStatus
        if review.decision == "accepted":
            target = "learning_writeback"
        elif review.decision == "partial_accepted":
            target = "partial_closed"
        else:
            target = "repairing"
        if mission.status != target:
            self.transition_mission(
                mission_id=review.mission_id,
                target=target,
                actor=actor,
                reason=f"acceptance review {review.decision}",
                subject_ref=review.acceptance_id,
            )
        mission = self._mission(review.mission_id)
        mission = mission.model_copy(update={"acceptance_ref": review.acceptance_id})
        self.missions[review.mission_id] = mission
        self._persist_mission(mission)
        self._record_ledger_event(
            mission_id=review.mission_id,
            event_type="acceptance",
            actor=actor,
            subject_ref=review.acceptance_id,
            payload={"decision": review.decision, "satisfaction": review.satisfaction},
        )

    def apply_capability_promotion(
        self,
        promotion: CapabilityPromotion,
        *,
        actor: str = "qi",
    ) -> CapabilityProfile | None:
        """Persist a Qi promotion decision without auto-loading non-production abilities.

        Approved replay/holdout/shadow/canary profiles are stored as evidence and
        rollback material, but ``list_default_runtime_capabilities`` is the only
        default KUN Runtime consumption path and exposes production profiles only.
        """

        gate = promotion.gate_evaluation
        profile = promotion.capability_profile
        self._validate_capability_promotion_boundary(promotion)
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        if promotion.decision == "approved" and profile is not None:
            self.capability_profiles[profile.capability_id] = profile
            self._persist_capability_profile(profile)
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="promotion",
            actor=actor,
            subject_ref=promotion.candidate_id,
            payload={
                "promotion_id": promotion.promotion_id,
                "candidate_id": promotion.candidate_id,
                "target_stage": promotion.target_stage,
                "decision": promotion.decision,
                "reason": promotion.reason,
                "capability_profile_ref": profile.capability_id if profile else "",
                "default_runtime_enabled": bool(
                    profile is not None and profile.promotion_stage == "production"
                ),
            },
            artifact_refs=promotion.evidence_refs,
        )
        return profile

    def _validate_capability_promotion_boundary(self, promotion: CapabilityPromotion) -> None:
        """Keep user-task learning evidence out of KUN production runtime defaults."""

        profile = promotion.capability_profile
        if (
            promotion.decision != "approved"
            or profile is None
            or profile.promotion_stage != "production"
            or not profile.runtime_enabled
        ):
            return
        gate = promotion.gate_evaluation
        mission = self.missions.get(gate.mission_id)
        if mission is None:
            raise ValueError(
                "production capability promotions require a registered self_improvement mission"
            )
        if mission.task_type != "self_improvement" or gate.task_type != "self_improvement":
            raise ValueError(
                "production capability promotions are only allowed from self_improvement "
                "Qi/Nuo governance missions; user task learning must stay a learning_signal "
                "or governance follow-up"
            )
        if gate.stage != "learning":
            raise ValueError("production capability promotions require a learning-stage gate")

    def list_default_runtime_capabilities(self) -> list[CapabilityProfile]:
        """Return governed production-stage capabilities for KUN Runtime default use."""

        profiles = [
            profile
            for profile in self.list_capability_profiles(stage="production")
            if profile.runtime_enabled
        ]
        governed, _report = govern_default_runtime_capabilities(profiles)
        return governed

    def govern_default_runtime_capabilities(self) -> CapabilityGovernanceReport:
        """Return the dedupe/source-version decisions for default runtime abilities."""

        _profiles, report = govern_default_runtime_capabilities(
            [
                profile
                for profile in self.list_capability_profiles(stage="production")
                if profile.runtime_enabled
            ]
        )
        return report

    def apply_capability_rollback(
        self,
        rollback: CapabilityRollback,
        *,
        actor: str = "qi",
    ) -> CapabilityProfile:
        """Disable a production/canary runtime ability and ledger the rollback."""

        profile = self.capability_profiles.get(rollback.capability_id)
        if profile is None:
            raise ValueError(f"capability profile not found: {rollback.capability_id}")
        gate = rollback.gate_evaluation
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        rolled_back = profile.model_copy(
            update={
                "runtime_enabled": False,
                "rolled_back_at": _now(),
                "rollback_reason": rollback.reason,
                "rollback_refs": [
                    rollback.rollback_id,
                    rollback.failed_evaluation_ref,
                    gate.gate_evaluation_id,
                ],
            }
        )
        self.capability_profiles[rolled_back.capability_id] = rolled_back
        self._persist_capability_profile(rolled_back)
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="rollback",
            actor=actor,
            subject_ref=rollback.capability_id,
            before={
                "runtime_enabled": profile.runtime_enabled,
                "promotion_stage": profile.promotion_stage,
            },
            after={
                "runtime_enabled": rolled_back.runtime_enabled,
                "promotion_stage": rolled_back.promotion_stage,
            },
            payload={
                "rollback_id": rollback.rollback_id,
                "capability_id": rollback.capability_id,
                "reason": rollback.reason,
                "failed_evaluation_ref": rollback.failed_evaluation_ref,
                "default_runtime_enabled": False,
            },
            artifact_refs=rollback.evidence_refs,
        )
        return rolled_back

    def list_capability_profiles(
        self,
        *,
        stage: CapabilityPromotionStage | None = None,
    ) -> list[CapabilityProfile]:
        """List stored capability profiles, optionally filtered by promotion stage."""

        profiles = list(self.capability_profiles.values())
        if stage is not None:
            profiles = [profile for profile in profiles if profile.promotion_stage == stage]
        return sorted(profiles, key=lambda profile: profile.capability_id)

    def progress_report(self, mission_id: str) -> ControlPlaneProgressReport:
        """Build a compact mission progress report from runtime state."""

        mission = self._mission(mission_id)
        items = self._mission_work_items(mission_id)
        counts = Counter(item.status for item in items)
        latest_gate = self._latest_gate(mission_id)
        latest_failed_run = self._latest_failed_run(mission_id)
        latest_failure_category = (
            latest_gate.failure_category
            if latest_gate and latest_gate.failure_category
            else latest_failed_run.failure_category
            if latest_failed_run and _failure_still_blocks_progress(mission.status, latest_gate)
            else None
        )
        return ControlPlaneProgressReport(
            mission_id=mission_id,
            status=mission.status,
            current_plan_version=mission.current_plan_version,
            total_work_items=len(items),
            work_item_counts={key: counts[key] for key in sorted(counts)},
            open_collaboration_ticket_ids=[
                ticket.ticket_id
                for ticket in self.collaboration_tickets.values()
                if ticket.mission_id == mission_id
                and ticket.status in {"open", "waiting", "escalated"}
            ],
            latest_gate_ref=latest_gate.gate_evaluation_id if latest_gate else None,
            latest_gate_action=latest_gate.next_action if latest_gate else None,
            latest_gate_verdict=latest_gate.north_star_verdict if latest_gate else None,
            latest_failure_category=latest_failure_category,
            next_ready_work_item_ids=[
                item.work_item_id for item in self._ready_work_items(mission_id)
            ],
            ledger_event_count=sum(
                1 for event in self.ledger_events.values() if event.mission_id == mission_id
            ),
            artifact_manifest_count=sum(
                1
                for manifest in self.artifact_manifests.values()
                if manifest.mission_id == mission_id
            ),
        )

    def _resume_after_collaboration(self, mission_id: str) -> None:
        open_tickets = [
            ticket
            for ticket in self.collaboration_tickets.values()
            if ticket.mission_id == mission_id and ticket.status in {"open", "waiting", "escalated"}
        ]
        if open_tickets:
            return
        for item in self._mission_work_items(mission_id):
            if item.status in {"waiting_human", "waiting_external"}:
                resumed = item.model_copy(update={"status": "queued"})
                self.work_items[item.work_item_id] = resumed
                self._persist_work_item(resumed)
        mission = self._mission(mission_id)
        if mission.status in {"waiting_human", "waiting_external", "escalated"}:
            self.transition_mission(
                mission_id=mission_id,
                target="queued",
                actor="control-plane",
                reason="collaboration response recorded; mission can resume",
            )

    def _complete_collaboration_work_items_for_response(
        self,
        *,
        ticket: CollaborationTicket,
        response: CollaborationResponse,
        actor: str,
    ) -> None:
        """Close matching human/operator work items after their ticket is resolved.

        Human collaboration work items are not daemon-runnable work. Once the
        associated ticket has an answer, leaving the item queued creates a
        runner_missing loop and blocks the actual product work behind it.
        """

        if response.status not in {"answered", "fallback_selected", "cancelled", "closed"}:
            return
        candidates = [
            item
            for item in self._mission_work_items(ticket.mission_id)
            if item.type == "collaboration"
            and item.status in {"queued", "waiting_human", "waiting_external", "blocked"}
        ]
        if not candidates:
            return
        matching = [
            item
            for item in candidates
            if self._collaboration_work_item_matches_ticket(
                item=item,
                ticket=ticket,
                all_candidates=candidates,
            )
        ]
        target_status = (
            "done" if response.status in {"answered", "fallback_selected"} else "cancelled"
        )
        for item in matching:
            updated = item.model_copy(update={"status": target_status})
            self.work_items[item.work_item_id] = updated
            self._persist_work_item(updated)
            run = RunRecord(
                work_item_id=item.work_item_id,
                runner_type="human",
                runner_identity=response.responder or actor,
                started_at=response.received_at,
                ended_at=response.received_at,
                exit_status="succeeded" if target_status == "done" else "cancelled",
            )
            self.runs[run.run_id] = run
            self._persist_run(run)
            self._record_ledger_event(
                mission_id=ticket.mission_id,
                event_type="state_change",
                actor=actor,
                subject_ref=item.work_item_id,
                payload={
                    "status": target_status,
                    "reason": f"collaboration ticket {ticket.ticket_id} resolved",
                    "ticket_id": ticket.ticket_id,
                    "response_status": response.status,
                },
            )

    @staticmethod
    def _collaboration_work_item_matches_ticket(
        *,
        item: WorkItem,
        ticket: CollaborationTicket,
        all_candidates: Sequence[WorkItem],
    ) -> bool:
        haystack = " ".join(
            [
                item.work_item_id,
                item.phase or "",
                item.expected_output,
                " ".join(item.recovery_refs),
                " ".join(item.checkpoint_refs),
                " ".join(item.external_source_refs),
            ]
        ).lower()
        explicit_ticket_refs = {
            ticket.ticket_id,
            ticket.context_ref,
            *ticket.auto_resolvable_by,
            *ticket.resolution_refs,
        }
        if any(ref and ref.lower() in haystack for ref in explicit_ticket_refs):
            return True
        if len(all_candidates) == 1:
            return True
        if ticket.type == "operator_action":
            return False
        if ticket.type in {"review", "acceptance_check"}:
            return any(token in haystack for token in ("review", "acceptance", "playtest"))
        return False

    def _mission(self, mission_id: str) -> Mission:
        try:
            return self.missions[mission_id]
        except KeyError as exc:
            raise ValueError(f"unknown mission {mission_id}") from exc

    def _mission_work_items(self, mission_id: str) -> list[WorkItem]:
        return [item for item in self.work_items.values() if item.mission_id == mission_id]

    def _runnable_plan_work_items(self, mission: Mission) -> list[WorkItem]:
        items = self._mission_work_items(mission.mission_id)
        if not mission.current_plan_version:
            return items
        return [item for item in items if item.task_plan_version == mission.current_plan_version]

    def _ready_work_items(
        self,
        mission_id: str,
        *,
        lease: str | None = None,
        now: datetime | None = None,
    ) -> list[WorkItem]:
        mission = self._mission(mission_id)
        observed_at = now or _now()
        done_ids = {
            item.work_item_id
            for item in self._mission_work_items(mission_id)
            if item.status == "done"
        }
        return sorted(
            (
                item
                for item in self._runnable_plan_work_items(mission)
                if item.status == "queued"
                and set(item.dependencies).issubset(done_ids)
                and self._stage_gate_allows_ready(mission=mission, work_item=item)
                and _lease_allows_ready(item, lease=lease, now=observed_at)
            ),
            key=lambda item: (-item.priority, item.work_item_id),
        )

    def _stage_gate_allows_ready(self, *, mission: Mission, work_item: WorkItem) -> bool:
        contract = self.contracts.get(mission.execution_contract_ref or "")
        if not _is_rainflow_context(mission=mission, contract=contract):
            return True
        required_gate_tokens = _rainflow_required_gate_tokens(work_item.work_item_id)
        if not required_gate_tokens:
            return True
        return _rainflow_stage_gate_passes_cleanly(
            gates=[
                gate
                for gate in self.gate_evaluations.values()
                if gate.mission_id == mission.mission_id
                and gate.task_plan_version == work_item.task_plan_version
            ],
            required_gate_tokens=required_gate_tokens,
        )

    def _latest_gate(self, mission_id: str) -> GateEvaluation | None:
        gates = {
            gate.gate_evaluation_id: gate
            for gate in self.gate_evaluations.values()
            if gate.mission_id == mission_id
        }
        gate_events = [
            event
            for event in self.ledger_events.values()
            if event.mission_id == mission_id
            and event.event_type == "gate_evaluation"
            and event.payload.get("gate_evaluation_id") in gates
        ]
        latest_event = max(gate_events, key=lambda event: event.sequence, default=None)
        if latest_event is not None:
            return gates[str(latest_event.payload["gate_evaluation_id"])]
        return max(gates.values(), key=lambda gate: gate.gate_evaluation_id, default=None)

    def _latest_failed_run(self, mission_id: str) -> RunRecord | None:
        work_item_ids = {item.work_item_id for item in self._mission_work_items(mission_id)}
        runs = [
            run
            for run in self.runs.values()
            if run.work_item_id in work_item_ids and run.exit_status == "failed"
        ]
        return max(runs, key=lambda run: run.run_id, default=None)

    def _collaboration_ticket_opened_event(self, ticket_id: str) -> LedgerEvent | None:
        events = [
            event
            for event in self.ledger_events.values()
            if event.subject_ref == ticket_id
            and event.event_type == "message"
            and event.payload.get("intent") != "collaboration_reminder"
        ]
        return min(events, key=lambda event: event.time, default=None)

    def _collaboration_reminder_already_sent(self, ticket_id: str) -> bool:
        return any(
            event.subject_ref == ticket_id
            and event.event_type == "message"
            and event.payload.get("intent") == "collaboration_reminder"
            for event in self.ledger_events.values()
        )

    def _record_ledger_event(
        self,
        *,
        mission_id: str,
        event_type: LedgerEventType,
        actor: str,
        subject_ref: str,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        artifact_refs: Iterable[str] = (),
    ) -> LedgerEvent:
        self._ledger_sequences[mission_id] += 1
        event = LedgerEvent(
            mission_id=mission_id,
            sequence=self._ledger_sequences[mission_id],
            event_type=event_type,
            actor=actor,
            correlation_id=mission_id,
            subject_ref=subject_ref,
            before=before or {},
            after=after or {},
            payload=payload or {},
            artifact_refs=list(artifact_refs),
            idempotency_key=f"{mission_id}:{self._ledger_sequences[mission_id]}:{event_type}",
        )
        self.ledger_events[event.event_id] = event
        self._persist_ledger_event(event)
        mission = self.missions.get(mission_id)
        if mission is not None:
            self.missions[mission_id] = mission.model_copy(
                update={"ledger_refs": [*mission.ledger_refs, event.event_id]}
            )
            self._persist_mission(self.missions[mission_id])
        return event

    def _hydrate_from_store(self, store: ControlPlaneStore) -> None:
        self.missions = {item.mission_id: item for item in store.list_missions()}
        self.task_plans = {item.plan_id: item for item in store.list_task_plans()}
        self.contracts = {item.contract_id: item for item in store.list_execution_contracts()}
        self.working_contexts = {
            item.working_context_id: item for item in store.list_working_contexts()
        }
        self.work_items = {item.work_item_id: item for item in store.list_work_items()}
        self.runs = {item.run_id: item for item in store.list_run_records()}
        self.artifacts = {item.artifact_id: item for item in store.list_artifact_records()}
        self.artifact_manifests = {
            item.manifest_id: item for item in store.list_artifact_manifests()
        }
        self.ledger_events = {item.event_id: item for item in store.list_ledger_events()}
        self.collaboration_tickets = {
            item.ticket_id: item for item in store.list_collaboration_tickets()
        }
        self.gate_evaluations = {
            item.gate_evaluation_id: item for item in store.list_gate_evaluations()
        }
        self.acceptance_reviews = {
            item.acceptance_id: item for item in store.list_acceptance_reviews()
        }
        self.capability_profiles = {
            item.capability_id: item for item in store.list_capability_profiles()
        }
        self._ledger_sequences = Counter(
            {
                mission_id: max(
                    (
                        event.sequence
                        for event in self.ledger_events.values()
                        if event.mission_id == mission_id
                    ),
                    default=0,
                )
                for mission_id in self.missions
            }
        )

    def _persist_mission(self, mission: Mission) -> None:
        if self.store is not None:
            self.store.put_mission(mission)

    def _persist_task_plan(self, task_plan: TaskPlan) -> None:
        if self.store is not None:
            self.store.put_task_plan(task_plan)

    def _persist_contract(self, contract: ExecutionContract) -> None:
        if self.store is not None:
            self.store.put_execution_contract(contract)

    def _persist_working_context(self, context: WorkingContext) -> None:
        if self.store is not None:
            self.store.put_working_context(context)

    def _persist_work_item(self, work_item: WorkItem) -> None:
        if self.store is not None:
            self.store.put_work_item(work_item)

    def _persist_run(self, run: RunRecord) -> None:
        if self.store is not None:
            self.store.put_run_record(run)

    def _persist_artifact(self, artifact: ArtifactRecord) -> None:
        if self.store is not None:
            self.store.put_artifact_record(artifact)

    def _persist_artifact_manifest(self, manifest: ArtifactManifest) -> None:
        if self.store is not None:
            self.store.put_artifact_manifest(manifest)

    def _persist_ledger_event(self, event: LedgerEvent) -> None:
        if self.store is not None:
            self.store.put_ledger_event(event)

    def _persist_collaboration_ticket(self, ticket: CollaborationTicket) -> None:
        if self.store is not None:
            self.store.put_collaboration_ticket(ticket)

    def _persist_gate(self, gate: GateEvaluation) -> None:
        if self.store is not None:
            self.store.put_gate_evaluation(gate)

    def _persist_acceptance_review(self, review: AcceptanceReview) -> None:
        if self.store is not None:
            self.store.put_acceptance_review(review)

    def _persist_capability_profile(self, profile: CapabilityProfile) -> None:
        if self.store is not None:
            self.store.put_capability_profile(profile)
