"""Qi AB round contract objects for the V6 control plane.

This module is intentionally a pure adapter.  It does not invoke agents,
comparators, commands, or benchmark runners.  It turns a completed Frontier50
round summary into V6 Control Plane objects so the runtime can persist the
round, gate it, repair it, or queue the next round.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kun.control_plane.runtime import WorkItemResult
from kun.control_plane.v6 import ArtifactManifest, FailureCategory, GateEvaluation, WorkItem

QI_AB_EXPECTED_ANSWER_COUNT = 20
QI_AB_EXPECTED_REVIEW_COUNT = 45
QI_AB_RUBRIC_VERSION = "qi-ab-frontier50-v6"
QI_AB_METRIC_PACK_VERSION = "qi-ab-round-gates-v1"

QiABRoundVerdict = Literal["pass", "repair", "invalid"]


class QiABRoundSummary(BaseModel):
    """Artifact-level summary emitted by a Frontier50 AB round runner."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str = Field(min_length=1)
    task_plan_version: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    task_ids: list[str] = Field(default_factory=list)
    answer_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    report_ref: str | None = None
    health_ref: str | None = None
    repair_ticket_refs: list[str] = Field(default_factory=list)
    comparator_healthy: bool = True
    kun_gate_passed: bool = False
    kun_result_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    speed: float = Field(default=0.5, ge=0.0, le=1.0)
    cost: float = Field(default=0.5, ge=0.0, le=1.0)
    expected_answer_count: int = Field(default=QI_AB_EXPECTED_ANSWER_COUNT, ge=1)
    expected_review_count: int = Field(default=QI_AB_EXPECTED_REVIEW_COUNT, ge=1)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ticket_gate_matches_kun_failure(self) -> QiABRoundSummary:
        if self.kun_gate_passed and self.repair_ticket_refs:
            raise ValueError("passing rounds must not carry KUN repair ticket refs")
        return self

    @property
    def artifact_refs(self) -> list[str]:
        refs = [*self.answer_refs, *self.review_refs]
        if self.report_ref:
            refs.append(self.report_ref)
        if self.health_ref:
            refs.append(self.health_ref)
        refs.extend(self.repair_ticket_refs)
        return refs

    @property
    def answer_count(self) -> int:
        return len(self.answer_refs)

    @property
    def review_count(self) -> int:
        return len(self.review_refs)


class QiABRoundControlPlaneContract(BaseModel):
    """V6 objects produced for one Qi AB round."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    round_id: str
    verdict: QiABRoundVerdict
    round_valid: bool
    agent_failure_counted: bool
    next_round_allowed: bool
    hard_gate_failures: list[str]
    artifact_manifest: ArtifactManifest
    gate_evaluation: GateEvaluation
    repair_work_item: WorkItem | None = None
    next_round_work_item: WorkItem | None = None
    work_item_result: WorkItemResult


def build_qi_ab_round_work_item(
    *,
    mission_id: str,
    task_plan_version: str,
    round_id: str,
    task_ids: Sequence[str],
    dependencies: Sequence[str] = (),
    priority: int = 80,
) -> WorkItem:
    """Represent one Frontier50 round as a queued V6 work item."""

    task_count = len(task_ids)
    return WorkItem(
        work_item_id=f"work-qi-ab-{round_id}",
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        type="test",
        owner="qi",
        dependencies=list(dependencies),
        priority=priority,
        expected_output=(
            f"Frontier50 {round_id}: run {task_count} tasks through Qi AB contract; "
            f"record {QI_AB_EXPECTED_ANSWER_COUNT} answers, "
            f"{QI_AB_EXPECTED_REVIEW_COUNT} reviews, report, health, and repair tickets. "
            "No external command is implied by this contract object."
        ),
    )


def build_qi_ab_round_contract(
    summary: QiABRoundSummary,
    *,
    next_round_id: str | None = None,
    next_round_task_ids: Sequence[str] = (),
) -> QiABRoundControlPlaneContract:
    """Turn a completed Frontier50 round summary into Control Plane objects."""

    hard_gate_failures = _hard_gate_failures(summary)
    round_valid = _round_is_valid(hard_gate_failures)
    verdict = _round_verdict(summary=summary, round_valid=round_valid)
    agent_failure_counted = verdict == "repair"
    manifest = _artifact_manifest(summary)
    repair_work_item = _repair_work_item(
        summary, verdict=verdict, hard_gate_failures=hard_gate_failures
    )
    next_round_work_item = (
        build_qi_ab_round_work_item(
            mission_id=summary.mission_id,
            task_plan_version=summary.task_plan_version,
            round_id=next_round_id,
            task_ids=next_round_task_ids,
            dependencies=[summary.work_item_id],
            priority=75,
        )
        if verdict == "pass" and next_round_id
        else None
    )
    gate = _gate_evaluation(
        summary=summary,
        verdict=verdict,
        round_valid=round_valid,
        agent_failure_counted=agent_failure_counted,
        hard_gate_failures=hard_gate_failures,
        manifest=manifest,
        repair_work_item=repair_work_item,
    )
    status: Literal["done", "failed"] = "done" if verdict == "pass" else "failed"
    result = WorkItemResult(
        status=status,
        summary=_result_summary(verdict=verdict, hard_gate_failures=hard_gate_failures),
        artifact_manifest=manifest,
        gate_evaluation=gate,
        failure_category=gate.failure_category,
        followup_work_items=[
            item for item in (repair_work_item, next_round_work_item) if item is not None
        ],
    )
    return QiABRoundControlPlaneContract(
        round_id=summary.round_id,
        verdict=verdict,
        round_valid=round_valid,
        agent_failure_counted=agent_failure_counted,
        next_round_allowed=verdict == "pass",
        hard_gate_failures=hard_gate_failures,
        artifact_manifest=manifest,
        gate_evaluation=gate,
        repair_work_item=repair_work_item,
        next_round_work_item=next_round_work_item,
        work_item_result=result,
    )


def _hard_gate_failures(summary: QiABRoundSummary) -> list[str]:
    failures: list[str] = []
    if summary.answer_count < summary.expected_answer_count:
        failures.append("answer_count_below_threshold")
    if summary.review_count < summary.expected_review_count:
        failures.append("review_count_below_threshold")
    if summary.report_ref is None:
        failures.append("report_missing")
    if summary.health_ref is None:
        failures.append("health_missing")
    if not summary.comparator_healthy:
        failures.append("comparator_unhealthy")
    if (
        summary.comparator_healthy
        and summary.answer_count >= summary.expected_answer_count
        and summary.review_count >= summary.expected_review_count
        and summary.report_ref is not None
        and summary.health_ref is not None
        and not summary.kun_gate_passed
        and not summary.repair_ticket_refs
    ):
        failures.append("repair_tickets_missing")
    return failures


def _round_is_valid(hard_gate_failures: Sequence[str]) -> bool:
    invalid_failures = {
        "answer_count_below_threshold",
        "review_count_below_threshold",
        "report_missing",
        "health_missing",
        "comparator_unhealthy",
        "repair_tickets_missing",
    }
    return not any(failure in invalid_failures for failure in hard_gate_failures)


def _round_verdict(*, summary: QiABRoundSummary, round_valid: bool) -> QiABRoundVerdict:
    if not round_valid:
        return "invalid"
    if summary.kun_gate_passed:
        return "pass"
    return "repair"


def _artifact_manifest(summary: QiABRoundSummary) -> ArtifactManifest:
    artifact_refs = _dedupe(summary.artifact_refs)
    return ArtifactManifest(
        manifest_id=f"manifest-qi-ab-{summary.round_id}",
        mission_id=summary.mission_id,
        work_item_id=summary.work_item_id,
        kind="run",
        artifact_refs=artifact_refs,
        primary_artifact_ref=summary.report_ref,
        evidence_refs=[summary.health_ref] if summary.health_ref is not None else [],
        review_refs=list(summary.review_refs),
        created_by="qi",
        content_hash=_content_hash(
            [
                summary.mission_id,
                summary.task_plan_version,
                summary.round_id,
                *artifact_refs,
            ]
        ),
        supports_delivery=False,
    )


def _gate_evaluation(
    *,
    summary: QiABRoundSummary,
    verdict: QiABRoundVerdict,
    round_valid: bool,
    agent_failure_counted: bool,
    hard_gate_failures: list[str],
    manifest: ArtifactManifest,
    repair_work_item: WorkItem | None,
) -> GateEvaluation:
    failure_category = _failure_category(verdict=verdict, hard_gate_failures=hard_gate_failures)
    result_quality = _result_quality(summary=summary, verdict=verdict)
    return GateEvaluation(
        gate_evaluation_id=f"gate-qi-ab-{summary.round_id}",
        mission_id=summary.mission_id,
        task_plan_version=summary.task_plan_version,
        subject_ref=summary.work_item_id,
        stage="workitem",
        task_type="self_improvement",
        rubric_version=QI_AB_RUBRIC_VERSION,
        metric_pack_version=QI_AB_METRIC_PACK_VERSION,
        north_star_verdict="pass" if verdict == "pass" else "fail",
        result_quality=result_quality,
        speed=summary.speed,
        cost=summary.cost,
        risk=0.2 if verdict == "pass" else 0.7,
        evidence_quality=1.0 if round_valid else 0.0,
        collaboration_quality=1.0
        if summary.review_count >= summary.expected_review_count
        else summary.review_count / summary.expected_review_count,
        score_breakdown={
            "answer_count": float(summary.answer_count),
            "review_count": float(summary.review_count),
            "report_present": 1.0 if summary.report_ref else 0.0,
            "health_present": 1.0 if summary.health_ref else 0.0,
            "comparator_healthy": 1.0 if summary.comparator_healthy else 0.0,
            "repair_ticket_count": float(len(summary.repair_ticket_refs)),
            "round_valid": 1.0 if round_valid else 0.0,
            "agent_failure_counted": 1.0 if agent_failure_counted else 0.0,
            "next_round_allowed": 1.0 if verdict == "pass" else 0.0,
        },
        thresholds={
            "result_quality": 0.8,
            "answer_count": float(summary.expected_answer_count),
            "review_count": float(summary.expected_review_count),
            "report_present": 1.0,
            "health_present": 1.0,
        },
        hard_gate_failures=hard_gate_failures,
        evidence_refs=manifest.evidence_refs,
        artifact_refs=manifest.artifact_refs,
        review_refs=manifest.review_refs,
        failure_category=failure_category,
        root_cause=_root_cause(verdict=verdict, hard_gate_failures=hard_gate_failures),
        responsibility_scope=_responsibility_scope(
            verdict=verdict,
            hard_gate_failures=hard_gate_failures,
        ),
        confidence=0.9 if verdict == "pass" else 0.8,
        next_action="continue" if verdict == "pass" else "needs_repair",
        next_state="running" if verdict == "pass" else "repairing",
        next_ticket_refs=[repair_work_item.work_item_id] if repair_work_item else [],
        learning_eligibility="candidate" if verdict == "repair" else "none",
        governance_signal="qi_ab_round_invalid" if verdict == "invalid" else "",
        created_by="qi",
    )


def _failure_category(
    *,
    verdict: QiABRoundVerdict,
    hard_gate_failures: Sequence[str],
) -> FailureCategory | None:
    if verdict == "pass":
        return None
    if "comparator_unhealthy" in hard_gate_failures:
        return "environment_failure"
    if verdict == "invalid":
        return "tool_failure"
    return "model_quality_failure"


def _result_quality(*, summary: QiABRoundSummary, verdict: QiABRoundVerdict) -> float:
    if verdict == "pass":
        return max(summary.kun_result_quality, 0.8)
    if verdict == "repair":
        return min(summary.kun_result_quality, 0.79)
    return 0.0


def _responsibility_scope(
    *,
    verdict: QiABRoundVerdict,
    hard_gate_failures: Sequence[str],
) -> Literal["kun_auto", "environment", "mixed"]:
    if verdict == "repair":
        return "kun_auto"
    if "comparator_unhealthy" in hard_gate_failures:
        return "environment"
    return "mixed"


def _root_cause(*, verdict: QiABRoundVerdict, hard_gate_failures: Sequence[str]) -> str:
    if verdict == "pass":
        return "qi_ab_round_passed"
    if "comparator_unhealthy" in hard_gate_failures:
        return "comparator health failed; round invalid and excluded from agent failure accounting"
    if verdict == "invalid":
        return "qi_ab_round_contract_incomplete"
    return "kun did not pass the Qi AB round gate"


def _repair_work_item(
    summary: QiABRoundSummary,
    *,
    verdict: QiABRoundVerdict,
    hard_gate_failures: Sequence[str],
) -> WorkItem | None:
    if verdict == "pass":
        return None
    owner = "nuo" if "comparator_unhealthy" in hard_gate_failures else "qi"
    expected_output = (
        "Repair comparator health and rerun the same Frontier50 round without counting an agent "
        "failure."
        if owner == "nuo"
        else _qi_repair_expected_output(summary=summary, hard_gate_failures=hard_gate_failures)
    )
    return WorkItem(
        work_item_id=f"work-qi-ab-repair-{summary.round_id}",
        mission_id=summary.mission_id,
        task_plan_version=summary.task_plan_version,
        type="repair",
        owner=owner,
        dependencies=[summary.work_item_id],
        priority=90,
        expected_output=expected_output,
    )


def _qi_repair_expected_output(
    *,
    summary: QiABRoundSummary,
    hard_gate_failures: Sequence[str],
) -> str:
    if "repair_tickets_missing" in hard_gate_failures:
        return (
            "Produce missing KUN repair tickets, bind them to replay evidence, then rerun the gate."
        )
    if hard_gate_failures:
        return f"Repair Qi round artifact contract failures: {', '.join(hard_gate_failures)}."
    ticket_refs = ", ".join(summary.repair_ticket_refs)
    return (
        "Apply KUN-only repair from Qi ticket artifacts, then run same-task replay before any next "
        f"round. Ticket refs: {ticket_refs}."
    )


def _result_summary(*, verdict: QiABRoundVerdict, hard_gate_failures: Sequence[str]) -> str:
    if verdict == "pass":
        return "Qi AB round passed; next round may be queued."
    if verdict == "repair":
        return "KUN failed the Qi AB round gate; repair work item generated."
    return f"Qi AB round invalid: {', '.join(hard_gate_failures)}."


def _content_hash(parts: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
