"""RSI-inspired, governed self-improvement loop for KUN Control Plane.

This module deliberately implements an engineering loop, not unrestricted
recursive self-improvement.  Nuo audits KUN behavior and code-facing signals,
Qi searches for multiple repair/evolution strategies, and any default runtime
change must still go through the existing capability promotion governance.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_governance import normalize_capability_governance_key
from kun.control_plane.runtime import InMemoryControlPlane, WorkItemResult
from kun.control_plane.v6 import (
    ArtifactRecord,
    CapabilityProfile,
    GateEvaluation,
    Mission,
    WorkItem,
)

NUO_SELF_IMPROVEMENT_OWNER = "nuo"
QI_SELF_IMPROVEMENT_OWNER = "qi"
SELF_IMPROVEMENT_AUDIT_SUPPORT = "nuo_self_improvement_audit"
SELF_IMPROVEMENT_STRATEGY_SUPPORT = "qi_self_improvement_strategy_search"

SelfImprovementGapCategory = Literal[
    "capability_gap",
    "coordination_gap",
    "safety_gap",
    "evaluation_gap",
    "recovery_gap",
    "efficiency_gap",
]
SelfImprovementSeverity = Literal["info", "low", "medium", "high", "critical"]
SelfImprovementStatus = Literal["clear", "needs_improvement", "blocked"]


class TaskPerformanceScore(BaseModel):
    """Compact historical mission score used by Nuo self-audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    task_type: str
    status: str
    total_work_items: int
    done_work_items: int
    failed_or_blocked_work_items: int
    pass_gate_count: int
    fail_gate_count: int
    score: float = Field(ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


class SelfImprovementGap(BaseModel):
    """One Nuo finding about KUN itself, not about a user deliverable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_id: str
    category: SelfImprovementGapCategory
    severity: SelfImprovementSeverity
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    recommended_owner: Literal["qi", "nuo", "mission-director", "kun", "human"] = "qi"
    recommended_action: str
    governance_boundary: str = (
        "user-task evidence may become learning_signal only; production capability changes "
        "require self_improvement promotion governance"
    )


class SelfImprovementAuditReport(BaseModel):
    """Nuo's whole-system audit report over code-facing and task-history signals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    mission_id: str
    task_plan_version: str
    scope: Literal["mission", "control_plane"] = "mission"
    status: SelfImprovementStatus
    summary: str
    task_scores: list[TaskPerformanceScore] = Field(default_factory=list)
    gaps: list[SelfImprovementGap] = Field(default_factory=list)
    inspected_signal_counts: dict[str, int] = Field(default_factory=dict)
    created_by: str = "nuo"

    @property
    def requires_qi_strategy_search(self) -> bool:
        return any(gap.severity in {"medium", "high", "critical"} for gap in self.gaps)


class SelfImprovementStrategyCandidate(BaseModel):
    """One Qi candidate strategy for a Nuo-discovered self-improvement gap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    gap_id: str
    hypothesis: str
    implementation_scope: str
    validation_plan: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    expected_impact: float = Field(ge=0.0, le=1.0)
    risk: float = Field(ge=0.0, le=1.0)
    selected: bool = False


class SelfImprovementStrategySearchReport(BaseModel):
    """Qi multi-strategy search output for governed KUN self-improvement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_id: str
    audit_ref: str
    mission_id: str
    task_plan_version: str
    candidates: list[SelfImprovementStrategyCandidate] = Field(default_factory=list)
    selected_candidate_refs: list[str] = Field(default_factory=list)
    promotion_policy: str = (
        "selected candidates become replay-stage evidence only until holdout, shadow, canary, "
        "rollback, and production promotion gates pass"
    )
    created_by: str = "qi"


class NuoSelfImprovementAuditRunner:
    """Run periodic Nuo audits over KUN's own coordination and capability signals."""

    runner_type: Literal["agent"] = "agent"
    runner_identity = "nuo-self-improvement-audit-runner"

    def __init__(self, *, control_plane: InMemoryControlPlane) -> None:
        self.control_plane = control_plane

    def can_run(self, work_item: WorkItem) -> bool:
        if work_item.owner != NUO_SELF_IMPROVEMENT_OWNER:
            return False
        if work_item.type not in {"governance", "review"}:
            return False
        return _is_self_improvement_audit_work(work_item)

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="Nuo self-improvement audit runner only handles Nuo audit work.",
                failure_category="tool_failure",
            )
        report = build_self_improvement_audit_report(
            self.control_plane,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
        )
        artifact = _artifact(
            work_item=work_item,
            created_by=self.runner_identity,
            support=SELF_IMPROVEMENT_AUDIT_SUPPORT,
            payload=report.model_dump(mode="json"),
            extra_supports=[
                "kun_self_improvement_gap_report",
                "self_improvement_governance_only",
                *[f"gap:{gap.category}" for gap in report.gaps],
                *[gap.gap_id for gap in report.gaps],
            ],
        )
        gate = _audit_gate(work_item=work_item, report=report, artifact=artifact)
        followups: list[WorkItem] = []
        if report.requires_qi_strategy_search:
            followups.append(_qi_strategy_search_work_item(work_item=work_item, report=report))
        return WorkItemResult(
            status="done",
            summary=report.summary,
            artifacts=[artifact],
            gate_evaluation=gate,
            followup_work_items=followups,
        )


class QiSelfImprovementStrategyRunner:
    """Run Qi multi-strategy search for Nuo self-improvement gaps."""

    runner_type: Literal["agent"] = "agent"
    runner_identity = "qi-self-improvement-strategy-runner"

    def __init__(self, *, control_plane: InMemoryControlPlane) -> None:
        self.control_plane = control_plane

    def can_run(self, work_item: WorkItem) -> bool:
        if work_item.owner != QI_SELF_IMPROVEMENT_OWNER:
            return False
        if work_item.type not in {"governance", "research"}:
            return False
        return _is_self_improvement_strategy_work(work_item)

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="Qi self-improvement strategy runner only handles Qi strategy work.",
                failure_category="tool_failure",
            )
        audit = build_self_improvement_audit_report(
            self.control_plane,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
        )
        strategy = build_qi_self_improvement_strategy_search(audit)
        artifact = _artifact(
            work_item=work_item,
            created_by=self.runner_identity,
            support=SELF_IMPROVEMENT_STRATEGY_SUPPORT,
            payload=strategy.model_dump(mode="json"),
            extra_supports=[
                "qi_multi_strategy_candidates",
                "qi_better_strategy_search",
                "self_improvement_governance_only",
                *strategy.selected_candidate_refs,
            ],
        )
        profile_ref: str | None = None
        if strategy.selected_candidate_refs:
            profile = _strategy_replay_profile(strategy=strategy, artifact=artifact)
            self.control_plane.capability_profiles[profile.capability_id] = profile
            if self.control_plane.store is not None:
                self.control_plane.store.put_capability_profile(profile)
            profile_ref = profile.capability_id
            artifact = artifact.model_copy(update={"supports": [*artifact.supports, profile_ref]})
        followups = _kun_implementation_work_items(work_item=work_item, strategy=strategy)
        gate = _strategy_gate(work_item=work_item, strategy=strategy, artifact=artifact)
        summary = (
            "Qi generated governed multi-strategy self-improvement candidates; selected "
            f"{len(strategy.selected_candidate_refs)} candidate(s)."
        )
        if profile_ref:
            summary += f" Replay profile: {profile_ref}."
        return WorkItemResult(
            status="done",
            summary=summary,
            artifacts=[artifact],
            gate_evaluation=gate,
            followup_work_items=followups,
        )


def build_self_improvement_audit_report(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    task_plan_version: str,
) -> SelfImprovementAuditReport:
    """Build a deterministic Nuo audit from current Control Plane state."""

    task_scores = _task_scores(control_plane)
    gaps = _dedupe_gaps(
        [
            *_capability_consumption_gaps(control_plane, mission_id),
            *_nuo_recovery_gaps(control_plane, mission_id),
            *_evaluation_gaps(control_plane, mission_id),
            *_coordination_gaps(control_plane, mission_id),
            *_capability_governance_gaps(control_plane),
            *_efficiency_gaps(control_plane, mission_id),
        ]
    )
    status: SelfImprovementStatus
    if any(gap.severity == "critical" for gap in gaps):
        status = "blocked"
    elif gaps:
        status = "needs_improvement"
    else:
        status = "clear"
    payload = {
        "mission_id": mission_id,
        "task_plan_version": task_plan_version,
        "gap_ids": [gap.gap_id for gap in gaps],
        "score_count": len(task_scores),
    }
    return SelfImprovementAuditReport(
        audit_id=f"nuo-self-audit-{_slug(mission_id)}-{_hash_payload(payload)[:12]}",
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        status=status,
        summary=(
            "Nuo self-improvement audit found no active KUN coordination gaps."
            if not gaps
            else f"Nuo self-improvement audit found {len(gaps)} KUN gap(s) requiring governance."
        ),
        task_scores=task_scores,
        gaps=gaps,
        inspected_signal_counts={
            "missions": len(control_plane.missions),
            "work_items": len(control_plane.work_items),
            "artifacts": len(control_plane.artifacts),
            "gates": len(control_plane.gate_evaluations),
            "capability_profiles": len(control_plane.capability_profiles),
        },
    )


def build_qi_self_improvement_strategy_search(
    audit: SelfImprovementAuditReport,
) -> SelfImprovementStrategySearchReport:
    """Generate multiple Qi strategies for every meaningful Nuo gap."""

    candidates: list[SelfImprovementStrategyCandidate] = []
    for gap in audit.gaps:
        candidates.extend(_strategy_candidates_for_gap(gap))
    selected = _select_strategy_candidates(candidates)
    return SelfImprovementStrategySearchReport(
        search_id=f"qi-self-strategy-{_slug(audit.audit_id)}",
        audit_ref=audit.audit_id,
        mission_id=audit.mission_id,
        task_plan_version=audit.task_plan_version,
        candidates=selected,
        selected_candidate_refs=[
            candidate.candidate_id for candidate in selected if candidate.selected
        ],
    )


def self_improvement_audit_signature(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
) -> str:
    """Stable signature used by the daemon to avoid duplicate audit work."""

    mission = control_plane.missions.get(mission_id)
    work_items = [
        item for item in control_plane.work_items.values() if item.mission_id == mission_id
    ]
    gates = [
        gate for gate in control_plane.gate_evaluations.values() if gate.mission_id == mission_id
    ]
    artifacts = [
        artifact
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id
    ]
    payload = {
        "mission_status": mission.status if mission else None,
        "task_plan_version": mission.current_plan_version if mission else None,
        "work_item_statuses": Counter(item.status for item in work_items),
        "work_item_count": len(work_items),
        "gate_count": len(gates),
        "artifact_count": len(artifacts),
        "capability_count": len(control_plane.capability_profiles),
    }
    return _hash_payload(payload)[:12]


def self_improvement_audit_work_item_id(mission_id: str, signature: str) -> str:
    return f"work-nuo-self-audit-{_slug(mission_id)}-{signature}"


def build_self_improvement_audit_work_item(
    *,
    mission: Mission,
    signature: str,
) -> WorkItem:
    task_plan_version = mission.current_plan_version or "self-improvement"
    return WorkItem(
        work_item_id=self_improvement_audit_work_item_id(mission.mission_id, signature),
        mission_id=mission.mission_id,
        task_plan_version=task_plan_version,
        type="governance",
        owner=NUO_SELF_IMPROVEMENT_OWNER,
        priority=82,
        idempotency_key=f"nuo-self-improvement-audit:{mission.mission_id}:{signature}",
        expected_output=(
            "Run a Nuo self improvement audit over KUN code-facing behavior, prior task "
            "scores, feature activation evidence, capability consumption, recovery closure, "
            "and governance boundaries. Produce gaps only; do not modify production runtime "
            "defaults from this audit."
        ),
        phase="self-improvement-audit",
    )


def _task_scores(control_plane: InMemoryControlPlane) -> list[TaskPerformanceScore]:
    scores: list[TaskPerformanceScore] = []
    for mission in control_plane.missions.values():
        work_items = [
            item
            for item in control_plane.work_items.values()
            if item.mission_id == mission.mission_id
        ]
        gates = [
            gate
            for gate in control_plane.gate_evaluations.values()
            if gate.mission_id == mission.mission_id
        ]
        failed_or_blocked = sum(
            1 for item in work_items if item.status in {"failed", "blocked", "partial"}
        )
        done = sum(1 for item in work_items if item.status == "done")
        pass_gates = sum(1 for gate in gates if gate.north_star_verdict == "pass")
        fail_gates = sum(1 for gate in gates if gate.north_star_verdict == "fail")
        score = 1.0
        if work_items:
            score -= min(0.45, failed_or_blocked / max(1, len(work_items)) * 0.6)
        if gates:
            score -= min(0.35, fail_gates / max(1, len(gates)) * 0.5)
        if (
            mission.status in {"awaiting_acceptance", "delivering"}
            and mission.acceptance_ref is None
        ):
            score -= 0.1
        notes: list[str] = []
        if failed_or_blocked:
            notes.append("failed_or_blocked_work_present")
        if fail_gates:
            notes.append("failed_gates_present")
        if (
            mission.status in {"awaiting_acceptance", "delivering"}
            and mission.acceptance_ref is None
        ):
            notes.append("delivery_without_acceptance")
        scores.append(
            TaskPerformanceScore(
                mission_id=mission.mission_id,
                task_type=mission.task_type,
                status=mission.status,
                total_work_items=len(work_items),
                done_work_items=done,
                failed_or_blocked_work_items=failed_or_blocked,
                pass_gate_count=pass_gates,
                fail_gate_count=fail_gates,
                score=max(0.0, round(score, 3)),
                notes=notes,
            )
        )
    return scores


def _capability_consumption_gaps(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[SelfImprovementGap]:
    receipt_work_item_ids = {
        artifact.work_item_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id
        and artifact.work_item_id is not None
        and "capability_behavior_receipt" in artifact.supports
    }
    missing = [
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.status in {"done", "partial"}
        and item.required_capability_refs
        and item.owner not in {"qi", "nuo"}
        and item.work_item_id not in receipt_work_item_ids
    ]
    if not missing:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-capability-consumption-{_hash_payload(missing)[:10]}",
            category="capability_gap",
            severity="high",
            summary="Required capabilities were attached to work items without behavior receipts.",
            evidence_refs=missing,
            recommended_action=(
                "Qi should design runner-level directive receipts and KUN should add tests proving "
                "capability policy changes execution behavior."
            ),
        )
    ]


def _nuo_recovery_gaps(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[SelfImprovementGap]:
    partial_nuo = [
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id
        and "nuo_runtime_repair_report" in artifact.supports
        and "nuo_runtime_repair_closed" not in artifact.supports
    ]
    clean_retests = {
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id and "nuo_clean_retest_passed" in artifact.supports
    }
    if not partial_nuo or clean_retests:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-nuo-clean-retest-{_hash_payload(partial_nuo)[:10]}",
            category="recovery_gap",
            severity="high",
            summary="Nuo has diagnosis evidence without clean retest closure evidence.",
            evidence_refs=partial_nuo,
            recommended_owner="nuo",
            recommended_action="Nuo must schedule and pass clean retest before recovery closes.",
        )
    ]


def _evaluation_gaps(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[SelfImprovementGap]:
    mission = control_plane.missions.get(mission_id)
    if mission is None:
        return []
    if mission.task_type != "product_development":
        return []
    if mission.status not in {"delivering", "awaiting_acceptance"}:
        return []
    supports = {
        support
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id
        for support in artifact.supports
    }
    missing: list[str] = []
    if mission.acceptance_ref is None:
        missing.append("human_acceptance_missing")
    if not supports.intersection(
        {"human_playtest", "target_user_playtest", "browser_interaction_replay"}
    ):
        missing.append("player_experience_evidence_missing")
    if not missing:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-evaluation-{_slug(mission_id)}-{_hash_payload(missing)[:8]}",
            category="evaluation_gap",
            severity="high",
            summary="Mission is near delivery without the required human/player experience evidence.",
            evidence_refs=missing,
            recommended_owner="mission-director",
            recommended_action=(
                "Mission Director should block closure and route Qi strategy replay or human "
                "acceptance before delivery can count as complete."
            ),
        )
    ]


def _coordination_gaps(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[SelfImprovementGap]:
    open_followups = [
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.owner in {"qi", "nuo", "mission-director"}
        and item.status in {"queued", "blocked", "partial", "failed"}
    ]
    if not open_followups:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-coordination-followups-{_hash_payload(open_followups)[:10]}",
            category="coordination_gap",
            severity="medium",
            summary="Governance follow-up work is still open and can stall task closure.",
            evidence_refs=open_followups,
            recommended_action=(
                "Qi/Nuo/Mission Director follow-ups need executable runners, dependency checks, "
                "and clean closure gates."
            ),
        )
    ]


def _capability_governance_gaps(
    control_plane: InMemoryControlPlane,
) -> list[SelfImprovementGap]:
    risky = [
        profile.capability_id
        for profile in control_plane.capability_profiles.values()
        if profile.runtime_enabled
        and (
            profile.promotion_stage != "production"
            or not profile.evidence_refs
            or not profile.rollback_plan
        )
    ]
    if not risky:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-runtime-profile-boundary-{_hash_payload(risky)[:10]}",
            category="safety_gap",
            severity="critical",
            summary="Runtime-enabled capability profiles lack production promotion evidence.",
            evidence_refs=risky,
            recommended_owner="qi",
            recommended_action=(
                "Demote unsafe profiles to replay/holdout or produce full promotion, holdout, "
                "regression, and rollback evidence."
            ),
        )
    ]


def _efficiency_gaps(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[SelfImprovementGap]:
    idempotency = Counter(
        item.idempotency_key
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id and item.idempotency_key
    )
    repeated = [key for key, count in idempotency.items() if count > 1]
    if not repeated:
        return []
    return [
        SelfImprovementGap(
            gap_id=f"gap-duplicate-work-{_hash_payload(repeated)[:10]}",
            category="efficiency_gap",
            severity="medium",
            summary="Duplicate idempotency keys indicate mechanical or repeated work generation.",
            evidence_refs=repeated,
            recommended_action=(
                "Mission Director should retire superseded branches and Qi should search for a "
                "different strategy instead of requeueing the same work."
            ),
        )
    ]


def _strategy_candidates_for_gap(
    gap: SelfImprovementGap,
) -> list[SelfImprovementStrategyCandidate]:
    prefix = f"cand-{_slug(gap.gap_id)}"
    common_validation = [
        "unit regression covering the original gap",
        "historical replay or fixture proving the old behavior fails",
        "Nuo clean retest or governance gate proving the gap is closed",
    ]
    return [
        SelfImprovementStrategyCandidate(
            candidate_id=f"{prefix}-instrument-and-block",
            gap_id=gap.gap_id,
            hypothesis=(
                "Add explicit instrumentation and hard gates so this gap cannot silently pass."
            ),
            implementation_scope="control-plane gate, artifact supports, and regression tests",
            validation_plan=common_validation,
            rollback_plan=["revert gate change and restore previous runner routing"],
            expected_impact=0.82,
            risk=0.28,
        ),
        SelfImprovementStrategyCandidate(
            candidate_id=f"{prefix}-runner-contract",
            gap_id=gap.gap_id,
            hypothesis=(
                "Move the behavior into a runner contract so activated capabilities are consumed "
                "by execution, not just metadata."
            ),
            implementation_scope="runner contract, sandbox/resource-lock checks, and receipts",
            validation_plan=[*common_validation, "daemon tick replay with the runner registered"],
            rollback_plan=["disable the new runner route and keep artifacts as learning_signal"],
            expected_impact=0.9,
            risk=0.38,
        ),
        SelfImprovementStrategyCandidate(
            candidate_id=f"{prefix}-replay-holdout",
            gap_id=gap.gap_id,
            hypothesis=(
                "Treat the fix as a capability candidate and prove it through replay/holdout "
                "before production defaults change."
            ),
            implementation_scope="Qi candidate, replay profile, holdout checklist, rollback plan",
            validation_plan=[*common_validation, "promotion remains runtime_enabled=false"],
            rollback_plan=["delete replay candidate if holdout fails"],
            expected_impact=0.76,
            risk=0.16,
        ),
    ]


def _select_strategy_candidates(
    candidates: list[SelfImprovementStrategyCandidate],
) -> list[SelfImprovementStrategyCandidate]:
    grouped: dict[str, list[SelfImprovementStrategyCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.gap_id, []).append(candidate)
    selected_ids: set[str] = set()
    for gap_candidates in grouped.values():
        best = max(
            gap_candidates,
            key=lambda item: (item.expected_impact - item.risk, item.expected_impact),
        )
        selected_ids.add(best.candidate_id)
    return [
        candidate.model_copy(update={"selected": candidate.candidate_id in selected_ids})
        for candidate in candidates
    ]


def _audit_gate(
    *,
    work_item: WorkItem,
    report: SelfImprovementAuditReport,
    artifact: ArtifactRecord,
) -> GateEvaluation:
    has_gaps = bool(report.gaps)
    return GateEvaluation(
        gate_evaluation_id=f"gate-nuo-self-audit-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="governance",
        task_type="self_improvement",
        rubric_version="kun-self-improvement-audit-v1",
        metric_pack_version="kun-self-improvement-loop-v1",
        north_star_verdict="pass",
        result_quality=0.86 if has_gaps else 0.94,
        speed=0.78,
        cost=0.86,
        risk=0.42 if has_gaps else 0.08,
        evidence_quality=0.88,
        collaboration_quality=0.82,
        score_breakdown={
            "gap_count": float(len(report.gaps)),
            "requires_qi_strategy_search": 1.0 if report.requires_qi_strategy_search else 0.0,
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=[],
        evidence_refs=[artifact.artifact_id],
        artifact_refs=[artifact.artifact_id],
        failure_category=None,
        responsibility_scope="kun_auto",
        confidence=0.86,
        next_action="needs_plan_change" if has_gaps else "continue",
        next_state="changing_plan" if has_gaps else "running",
        learning_eligibility="none",
        governance_signal="nuo_self_improvement_gaps_found"
        if has_gaps
        else "nuo_self_improvement_clear",
        created_by="nuo",
    )


def _strategy_gate(
    *,
    work_item: WorkItem,
    strategy: SelfImprovementStrategySearchReport,
    artifact: ArtifactRecord,
) -> GateEvaluation:
    has_selection = bool(strategy.selected_candidate_refs)
    return GateEvaluation(
        gate_evaluation_id=f"gate-qi-self-strategy-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="learning",
        task_type="self_improvement",
        rubric_version="kun-self-improvement-strategy-v1",
        metric_pack_version="kun-self-improvement-loop-v1",
        north_star_verdict="pass" if has_selection else "partial",
        result_quality=0.88 if has_selection else 0.7,
        speed=0.72,
        cost=0.82,
        risk=0.24 if has_selection else 0.44,
        evidence_quality=0.84,
        collaboration_quality=0.82,
        score_breakdown={
            "candidate_count": float(len(strategy.candidates)),
            "selected_candidate_count": float(len(strategy.selected_candidate_refs)),
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=[] if has_selection else ["no_self_improvement_strategy_selected"],
        evidence_refs=[artifact.artifact_id],
        artifact_refs=[artifact.artifact_id],
        failure_category=None if has_selection else "plan_failure",
        responsibility_scope="kun_auto",
        confidence=0.84,
        next_action="continue" if has_selection else "needs_plan_change",
        next_state="running" if has_selection else "changing_plan",
        learning_eligibility="ready_for_shadow" if has_selection else "candidate",
        governance_signal="qi_self_improvement_strategy_selected"
        if has_selection
        else "qi_self_improvement_strategy_missing",
        created_by="qi",
    )


def _qi_strategy_search_work_item(
    *,
    work_item: WorkItem,
    report: SelfImprovementAuditReport,
) -> WorkItem:
    suffix = _hash_payload(
        {"audit_id": report.audit_id, "gaps": [gap.gap_id for gap in report.gaps]}
    )[:12]
    return WorkItem(
        work_item_id=f"work-qi-self-strategy-{_slug(work_item.mission_id)}-{suffix}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        type="governance",
        owner=QI_SELF_IMPROVEMENT_OWNER,
        priority=min(100, work_item.priority + 5),
        dependencies=[work_item.work_item_id],
        idempotency_key=f"qi-self-improvement-strategy:{report.audit_id}",
        expected_output=(
            "Run Qi multi-strategy search for the Nuo self improvement audit. Generate at "
            "least two candidate fixes per gap, select the best strategy by quality/risk/cost, "
            "emit replay-stage evidence only, and do not enable production runtime defaults."
        ),
        recovery_refs=[report.audit_id, *[gap.gap_id for gap in report.gaps]],
        phase="self-improvement-strategy-search",
    )


def _kun_implementation_work_items(
    *,
    work_item: WorkItem,
    strategy: SelfImprovementStrategySearchReport,
) -> list[WorkItem]:
    if not strategy.selected_candidate_refs:
        return []
    suffix = _hash_payload(strategy.selected_candidate_refs)[:12]
    return [
        WorkItem(
            work_item_id=f"work-kun-self-improvement-implementation-{_slug(work_item.mission_id)}-{suffix}",
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="execution",
            owner="kun",
            priority=min(100, work_item.priority + 6),
            dependencies=[work_item.work_item_id],
            idempotency_key=f"kun-self-improvement-implementation:{strategy.search_id}",
            expected_output=(
                "Implement the selected KUN self-improvement candidate in a branch/sandbox with "
                "explicit rollback, regression tests, historical replay, and Nuo clean retest. "
                "This work may produce capability candidates only; production runtime defaults "
                "remain disabled until governance promotion passes."
            ),
            recovery_refs=[strategy.search_id, *strategy.selected_candidate_refs],
            phase="self-improvement-implementation",
        )
    ]


def _strategy_replay_profile(
    *,
    strategy: SelfImprovementStrategySearchReport,
    artifact: ArtifactRecord,
) -> CapabilityProfile:
    return CapabilityProfile(
        capability_id=f"cap-self-improvement-{_slug(strategy.search_id)}",
        capability_name="KUN governed self-improvement strategy candidate",
        governance_key=normalize_capability_governance_key("kun governed self improvement loop"),
        source_refs=[strategy.audit_ref],
        source_versions=["kun-self-improvement-strategy-v1"],
        evidence_refs=[artifact.artifact_id, *strategy.selected_candidate_refs],
        known_limits=[
            "replay evidence only; not production default",
            "requires holdout, shadow, canary, rollback, and governance promotion",
            "ordinary user tasks cannot enable this runtime behavior directly",
        ],
        promotion_stage="replay",
        rollback_plan=["delete or supersede replay profile if validation fails"],
        runtime_enabled=False,
    )


def _artifact(
    *,
    work_item: WorkItem,
    created_by: str,
    support: str,
    payload: dict[str, object],
    extra_supports: list[str] | None = None,
) -> ArtifactRecord:
    digest = _hash_payload(payload)
    return ArtifactRecord(
        artifact_id=f"artifact-{_slug(created_by)}-{_slug(work_item.work_item_id)}-{digest[:12]}",
        kind="report",
        path_or_uri=(
            f"control-plane://self-improvement/{created_by}/"
            f"{work_item.mission_id}/{work_item.work_item_id}/{digest[:12]}"
        ),
        content_hash=digest,
        created_by=created_by,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[support, *(extra_supports or [])],
        freshness="fresh",
        source_quality="primary",
    )


def _dedupe_gaps(gaps: list[SelfImprovementGap]) -> list[SelfImprovementGap]:
    by_id: dict[str, SelfImprovementGap] = {}
    for gap in gaps:
        by_id.setdefault(gap.gap_id, gap)
    return list(by_id.values())


def _is_self_improvement_audit_work(work_item: WorkItem) -> bool:
    key = work_item.idempotency_key or ""
    text = f"{work_item.phase or ''}\n{work_item.expected_output}".lower()
    return key.startswith("nuo-self-improvement-audit:") or any(
        token in text
        for token in (
            "self improvement audit",
            "self-improvement audit",
            "自我改进审计",
            "自我进化审计",
            "全局审计",
        )
    )


def _is_self_improvement_strategy_work(work_item: WorkItem) -> bool:
    key = work_item.idempotency_key or ""
    text = f"{work_item.phase or ''}\n{work_item.expected_output}".lower()
    return key.startswith("qi-self-improvement-strategy:") or any(
        token in text
        for token in (
            "multi-strategy search",
            "self improvement strategy",
            "self-improvement strategy",
            "多策略搜索",
            "自我改进策略",
            "自我进化策略",
        )
    )


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _slug(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return safe.strip("-")[:96] or "item"


__all__ = [
    "NUO_SELF_IMPROVEMENT_OWNER",
    "QI_SELF_IMPROVEMENT_OWNER",
    "SELF_IMPROVEMENT_AUDIT_SUPPORT",
    "SELF_IMPROVEMENT_STRATEGY_SUPPORT",
    "NuoSelfImprovementAuditRunner",
    "QiSelfImprovementStrategyRunner",
    "SelfImprovementAuditReport",
    "SelfImprovementGap",
    "SelfImprovementStrategyCandidate",
    "SelfImprovementStrategySearchReport",
    "TaskPerformanceScore",
    "build_qi_self_improvement_strategy_search",
    "build_self_improvement_audit_report",
    "build_self_improvement_audit_work_item",
    "self_improvement_audit_signature",
    "self_improvement_audit_work_item_id",
]
