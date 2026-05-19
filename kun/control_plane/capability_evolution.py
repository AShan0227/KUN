"""Qi capability evolution contract for the V6 control plane.

Qi owns capability discovery and verification, but it must not write new
runtime capability directly into production.  This module keeps the contract
pure: callers provide candidate evidence, staged evaluation records, and an
optional Nuo health report; the output is a promotion decision and, only when
all gates pass, a V6 ``CapabilityProfile`` that KUN Runtime can consume.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kun.control_plane.nuo import NuoHealthReport
from kun.control_plane.v6 import CapabilityProfile, FailureCategory, GateEvaluation, TaskType

CapabilitySource = Literal[
    "ab_gap",
    "real_task_review",
    "enterprise_project",
    "open_source_project",
    "expert_input",
]
CapabilityPromotionStage = Literal["replay", "holdout", "shadow", "canary", "production"]
CapabilityPromotionDecision = Literal["approved", "blocked"]

CAPABILITY_EVOLUTION_RUBRIC_VERSION = "qi-capability-evolution-v6"
CAPABILITY_EVOLUTION_METRIC_PACK_VERSION = "north-star-capability-v1"

_STAGE_ORDER: dict[CapabilityPromotionStage, int] = {
    "replay": 0,
    "holdout": 1,
    "shadow": 2,
    "canary": 3,
    "production": 4,
}


def _now() -> datetime:
    return datetime.now(UTC)


class CapabilityCandidate(BaseModel):
    """One Qi learning candidate before it is trusted by KUN Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    capability_name: str = Field(min_length=1)
    source: CapabilitySource
    source_ref: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    target_task_types: list[TaskType] = Field(default_factory=list)
    proposed_change_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    known_limits: list[str] = Field(default_factory=list)
    created_by: str = "qi"

    @model_validator(mode="after")
    def _candidate_needs_runtime_consumer_and_trace(self) -> CapabilityCandidate:
        if not self.target_task_types:
            raise ValueError("capability candidates require at least one target_task_type")
        if not (self.proposed_change_refs or self.evidence_refs):
            raise ValueError("capability candidates require proposed_change_refs or evidence_refs")
        return self


class CapabilityEvaluation(BaseModel):
    """Verification result for one promotion stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    stage: CapabilityPromotionStage
    mission_id: str = Field(min_length=1)
    task_plan_version: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    passed: bool
    result_quality: float = Field(ge=0.0, le=1.0)
    speed: float = Field(ge=0.0, le=1.0)
    cost: float = Field(ge=0.0, le=1.0)
    risk: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    holdout_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    hard_gate_failures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _passed_evaluations_need_north_star_and_stage_evidence(self) -> CapabilityEvaluation:
        if not self.passed:
            return self
        if self.hard_gate_failures:
            raise ValueError("passed capability evaluations cannot have hard_gate_failures")
        if self.result_quality < 0.8:
            raise ValueError("capability promotion cannot offset low result quality")
        if not self.evidence_refs:
            raise ValueError("passed capability evaluations require evidence_refs")
        if self.stage == "holdout" and not self.holdout_refs:
            raise ValueError("passed holdout evaluations require holdout_refs")
        if self.stage in {"canary", "production"} and not (
            self.holdout_refs and self.regression_refs and self.rollback_plan
        ):
            raise ValueError(
                "passed canary/production evaluations require holdout, regression, and rollback"
            )
        return self


class CapabilityPromotion(BaseModel):
    """Control-plane promotion decision for one capability candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    promotion_id: str
    candidate_id: str
    target_stage: CapabilityPromotionStage
    decision: CapabilityPromotionDecision
    reason: str
    evaluation_refs: list[str] = Field(default_factory=list)
    hard_gate_failures: list[str] = Field(default_factory=list)
    blocked_by_nuo: bool = False
    nuo_finding_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    holdout_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    gate_evaluation: GateEvaluation
    capability_profile: CapabilityProfile | None = None

    @model_validator(mode="after")
    def _approved_promotions_need_profile(self) -> CapabilityPromotion:
        if self.decision == "approved" and self.capability_profile is None:
            raise ValueError("approved capability promotions require capability_profile")
        if self.decision == "blocked" and self.capability_profile is not None:
            raise ValueError("blocked capability promotions must not include capability_profile")
        return self


class CapabilityRollback(BaseModel):
    """Auditable rollback decision for a production or canary runtime ability."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    rollback_id: str
    capability_id: str
    reason: str
    failed_evaluation_ref: str
    evidence_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    gate_evaluation: GateEvaluation


def build_capability_promotion(
    candidate: CapabilityCandidate,
    evaluations: Sequence[CapabilityEvaluation],
    *,
    target_stage: CapabilityPromotionStage,
    nuo_report: NuoHealthReport | None = None,
    capability_id: str | None = None,
) -> CapabilityPromotion:
    """Evaluate Qi staged evidence and return a promotion decision."""

    relevant_evaluations = [item for item in evaluations if item.candidate_id == candidate.candidate_id]
    if len(relevant_evaluations) != len(evaluations):
        raise ValueError("all capability evaluations must belong to the candidate")

    selected = _selected_passed_evaluations(relevant_evaluations, target_stage=target_stage)
    evidence_refs = _dedupe([*candidate.evidence_refs, *[ref for item in selected for ref in item.evidence_refs]])
    holdout_refs = _dedupe([ref for item in selected for ref in item.holdout_refs])
    regression_refs = _dedupe([ref for item in selected for ref in item.regression_refs])
    rollback_plan = _dedupe([step for item in selected for step in item.rollback_plan])
    evaluation_refs = [item.evaluation_id for item in selected]

    block_reason = _nuo_block_reason(nuo_report)
    hard_gate_failures: list[str] = []
    blocked_by_nuo = block_reason is not None
    if block_reason is None:
        missing_stages = _missing_required_stages(relevant_evaluations, target_stage=target_stage)
        if missing_stages:
            hard_gate_failures.extend(f"{stage}_evaluation_missing_or_failed" for stage in missing_stages)
            block_reason = f"missing passed stages: {', '.join(missing_stages)}"
    if block_reason is None and target_stage in {"canary", "production"}:
        missing_proof = _missing_production_proof(
            evidence_refs=evidence_refs,
            holdout_refs=holdout_refs,
            regression_refs=regression_refs,
            rollback_plan=rollback_plan,
        )
        if missing_proof:
            hard_gate_failures.extend(missing_proof)
            block_reason = f"missing promotion proof: {', '.join(missing_proof)}"

    if block_reason is not None:
        nuo_finding_refs = (
            [finding.finding_id for finding in nuo_report.findings]
            if nuo_report is not None and blocked_by_nuo
            else []
        )
        gate = _promotion_gate(
            candidate=candidate,
            evaluations=relevant_evaluations,
            target_stage=target_stage,
            decision="blocked",
            reason=block_reason,
            evidence_refs=evidence_refs,
            holdout_refs=holdout_refs,
            regression_refs=regression_refs,
            hard_gate_failures=[
                *hard_gate_failures,
                *([finding.code for finding in nuo_report.findings] if blocked_by_nuo and nuo_report else []),
            ],
            failure_category=_failure_category(blocked_by_nuo=blocked_by_nuo, hard_gate_failures=hard_gate_failures),
        )
        return CapabilityPromotion(
            promotion_id=f"promotion-{candidate.candidate_id}-{target_stage}",
            candidate_id=candidate.candidate_id,
            target_stage=target_stage,
            decision="blocked",
            reason=block_reason,
            evaluation_refs=evaluation_refs,
            hard_gate_failures=gate.hard_gate_failures,
            blocked_by_nuo=blocked_by_nuo,
            nuo_finding_refs=nuo_finding_refs,
            evidence_refs=evidence_refs,
            holdout_refs=holdout_refs,
            regression_refs=regression_refs,
            rollback_plan=rollback_plan,
            gate_evaluation=gate,
        )

    profile = CapabilityProfile(
        capability_id=capability_id or f"cap-{candidate.candidate_id}",
        capability_name=candidate.capability_name,
        evidence_refs=evidence_refs,
        known_limits=list(candidate.known_limits),
        promotion_stage=target_stage,
        holdout_refs=holdout_refs,
        regression_refs=regression_refs,
        last_verified_at=_now(),
        rollback_plan=rollback_plan,
    )
    gate = _promotion_gate(
        candidate=candidate,
        evaluations=selected,
        target_stage=target_stage,
        decision="approved",
        reason=f"passed Qi capability evolution gates through {target_stage}",
        evidence_refs=evidence_refs,
        holdout_refs=holdout_refs,
        regression_refs=regression_refs,
        hard_gate_failures=[],
        failure_category=None,
    )
    return CapabilityPromotion(
        promotion_id=f"promotion-{candidate.candidate_id}-{target_stage}",
        candidate_id=candidate.candidate_id,
        target_stage=target_stage,
        decision="approved",
        reason=f"passed Qi capability evolution gates through {target_stage}",
        evaluation_refs=evaluation_refs,
        evidence_refs=evidence_refs,
        holdout_refs=holdout_refs,
        regression_refs=regression_refs,
        rollback_plan=rollback_plan,
        gate_evaluation=gate,
        capability_profile=profile,
    )


def build_capability_rollback(
    profile: CapabilityProfile,
    failed_evaluation: CapabilityEvaluation,
    *,
    reason: str | None = None,
) -> CapabilityRollback:
    """Build the rollback decision that removes a bad runtime ability by default."""

    if profile.promotion_stage not in {"canary", "production"}:
        raise ValueError("only canary/production capability profiles can use runtime rollback")
    if failed_evaluation.passed and not failed_evaluation.hard_gate_failures:
        raise ValueError("capability rollback requires a failed evaluation or hard gate failure")
    rollback_reason = reason or (
        f"capability {profile.capability_id} failed {failed_evaluation.stage} verification"
    )
    hard_gate_failures = failed_evaluation.hard_gate_failures or [
        "production_runtime_regression"
    ]
    evidence_refs = _dedupe([*profile.evidence_refs, *failed_evaluation.evidence_refs])
    regression_refs = _dedupe([*profile.regression_refs, *failed_evaluation.regression_refs])
    artifact_refs = _dedupe([*failed_evaluation.artifact_refs, *evidence_refs])
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-{profile.capability_id}-rollback",
        mission_id=failed_evaluation.mission_id,
        task_plan_version=failed_evaluation.task_plan_version,
        subject_ref=profile.capability_id,
        stage="learning",
        task_type="self_improvement",
        rubric_version=CAPABILITY_EVOLUTION_RUBRIC_VERSION,
        metric_pack_version=CAPABILITY_EVOLUTION_METRIC_PACK_VERSION,
        north_star_verdict="fail",
        result_quality=failed_evaluation.result_quality,
        speed=failed_evaluation.speed,
        cost=failed_evaluation.cost,
        risk=max(failed_evaluation.risk, 0.8),
        evidence_quality=1.0 if evidence_refs else 0.0,
        collaboration_quality=1.0,
        score_breakdown={
            "failed_stage_index": float(_STAGE_ORDER[failed_evaluation.stage]),
            "rollback_plan_step_count": float(len(profile.rollback_plan)),
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=hard_gate_failures,
        evidence_refs=evidence_refs,
        artifact_refs=artifact_refs,
        test_refs=regression_refs,
        review_refs=list(failed_evaluation.review_refs),
        failure_category=_rollback_failure_category(failed_evaluation),
        root_cause=rollback_reason,
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="rollback_capability",
        next_state="rolling_back",
        learning_eligibility="blocked",
        governance_signal="qi_capability_rollback",
        created_by="qi",
    )
    return CapabilityRollback(
        rollback_id=f"rollback-{profile.capability_id}-{failed_evaluation.evaluation_id}",
        capability_id=profile.capability_id,
        reason=rollback_reason,
        failed_evaluation_ref=failed_evaluation.evaluation_id,
        evidence_refs=evidence_refs,
        regression_refs=regression_refs,
        rollback_plan=list(profile.rollback_plan),
        gate_evaluation=gate,
    )


def _selected_passed_evaluations(
    evaluations: Sequence[CapabilityEvaluation],
    *,
    target_stage: CapabilityPromotionStage,
) -> list[CapabilityEvaluation]:
    required = set(_required_stages(target_stage))
    selected: list[CapabilityEvaluation] = []
    for stage in required:
        stage_evaluations = [item for item in evaluations if item.stage == stage and item.passed]
        if stage_evaluations:
            selected.append(stage_evaluations[-1])
    return sorted(selected, key=lambda item: _STAGE_ORDER[item.stage])


def _missing_required_stages(
    evaluations: Sequence[CapabilityEvaluation],
    *,
    target_stage: CapabilityPromotionStage,
) -> list[CapabilityPromotionStage]:
    missing: list[CapabilityPromotionStage] = []
    for stage in _required_stages(target_stage):
        if not any(item.stage == stage and item.passed for item in evaluations):
            missing.append(stage)
    return missing


def _required_stages(target_stage: CapabilityPromotionStage) -> list[CapabilityPromotionStage]:
    target_index = _STAGE_ORDER[target_stage]
    return [stage for stage, index in _STAGE_ORDER.items() if index <= target_index]


def _missing_production_proof(
    *,
    evidence_refs: Sequence[str],
    holdout_refs: Sequence[str],
    regression_refs: Sequence[str],
    rollback_plan: Sequence[str],
) -> list[str]:
    missing: list[str] = []
    if not evidence_refs:
        missing.append("evidence_refs_missing")
    if not holdout_refs:
        missing.append("holdout_refs_missing")
    if not regression_refs:
        missing.append("regression_refs_missing")
    if not rollback_plan:
        missing.append("rollback_plan_missing")
    return missing


def _nuo_block_reason(nuo_report: NuoHealthReport | None) -> str | None:
    if nuo_report is None or nuo_report.status != "blocked":
        return None
    codes = ", ".join(finding.code for finding in nuo_report.findings)
    return f"Nuo blocked capability promotion: {codes}"


def _promotion_gate(
    *,
    candidate: CapabilityCandidate,
    evaluations: Sequence[CapabilityEvaluation],
    target_stage: CapabilityPromotionStage,
    decision: CapabilityPromotionDecision,
    reason: str,
    evidence_refs: Sequence[str],
    holdout_refs: Sequence[str],
    regression_refs: Sequence[str],
    hard_gate_failures: Sequence[str],
    failure_category: FailureCategory | None,
) -> GateEvaluation:
    passed = decision == "approved"
    result_quality = min((item.result_quality for item in evaluations), default=0.0)
    speed = min((item.speed for item in evaluations), default=0.0)
    cost = min((item.cost for item in evaluations), default=0.0)
    risk = max((item.risk for item in evaluations), default=1.0)
    artifact_refs = _dedupe([ref for item in evaluations for ref in item.artifact_refs])
    review_refs = _dedupe([ref for item in evaluations for ref in item.review_refs])
    return GateEvaluation(
        gate_evaluation_id=f"gate-{candidate.candidate_id}-{target_stage}",
        mission_id=evaluations[-1].mission_id if evaluations else candidate.source_ref,
        task_plan_version=evaluations[-1].task_plan_version if evaluations else "capability-evolution",
        subject_ref=candidate.candidate_id,
        stage="learning",
        task_type="self_improvement",
        rubric_version=CAPABILITY_EVOLUTION_RUBRIC_VERSION,
        metric_pack_version=CAPABILITY_EVOLUTION_METRIC_PACK_VERSION,
        north_star_verdict="pass" if passed else "fail",
        result_quality=result_quality if passed else min(result_quality, 0.79),
        speed=speed,
        cost=cost,
        risk=risk if passed else 1.0,
        evidence_quality=1.0 if passed else 0.0,
        collaboration_quality=1.0,
        score_breakdown={
            "target_stage": float(_STAGE_ORDER[target_stage]),
            "passed_stage_count": float(len([item for item in evaluations if item.passed])),
            "evidence_ref_count": float(len(evidence_refs)),
            "holdout_ref_count": float(len(holdout_refs)),
            "regression_ref_count": float(len(regression_refs)),
        },
        thresholds={
            "result_quality": 0.8,
            "required_stage_index": float(_STAGE_ORDER[target_stage]),
        },
        hard_gate_failures=list(hard_gate_failures),
        evidence_refs=list(evidence_refs),
        artifact_refs=artifact_refs or list(evidence_refs),
        review_refs=review_refs,
        test_refs=list(regression_refs),
        failure_category=failure_category,
        root_cause=reason,
        responsibility_scope="kun_auto" if failure_category else "unknown",
        confidence=0.9 if passed else 0.75,
        next_action="promote_candidate" if passed else "needs_repair",
        next_state="learning_writeback" if passed else "repairing",
        learning_eligibility="ready_for_shadow"
        if passed and target_stage in {"shadow", "canary", "production"}
        else ("candidate" if passed else "blocked"),
        governance_signal=f"qi_capability_{decision}",
        created_by="qi",
    )


def _failure_category(
    *,
    blocked_by_nuo: bool,
    hard_gate_failures: Sequence[str],
) -> FailureCategory:
    if blocked_by_nuo:
        return "tool_failure"
    if any(failure.endswith("_missing") for failure in hard_gate_failures):
        return "evidence_failure"
    return "model_quality_failure"


def _rollback_failure_category(evaluation: CapabilityEvaluation) -> FailureCategory:
    if any("evidence" in failure for failure in evaluation.hard_gate_failures):
        return "evidence_failure"
    if any("permission" in failure for failure in evaluation.hard_gate_failures):
        return "permission_failure"
    if any("timeout" in failure or "tool" in failure for failure in evaluation.hard_gate_failures):
        return "tool_failure"
    return "model_quality_failure"


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "CAPABILITY_EVOLUTION_METRIC_PACK_VERSION",
    "CAPABILITY_EVOLUTION_RUBRIC_VERSION",
    "CapabilityCandidate",
    "CapabilityEvaluation",
    "CapabilityPromotion",
    "CapabilityPromotionDecision",
    "CapabilityPromotionStage",
    "CapabilityRollback",
    "CapabilitySource",
    "build_capability_promotion",
    "build_capability_rollback",
]
