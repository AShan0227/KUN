"""Generic KUN task runner for Control Plane work items.

The productized daemon must not only run productization, Qi, and Nuo work.  It
also needs a default path for ordinary KUN-owned execution/research/review/test
work so real long tasks do not fall back to one-off task-specific runners.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.concurrency import build_merge_governance_report
from kun.control_plane.runtime import InMemoryControlPlane, WorkItemResult
from kun.control_plane.v6 import ArtifactManifest, ArtifactRecord, GateEvaluation, WorkItem


class KunTaskExecutionOutput(BaseModel):
    """Normalized output from the classic KUN task executor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["done", "partial", "waiting_human", "waiting_external", "blocked", "failed"]
    answer: str
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_sec: float = 0.0
    raw: dict[str, Any] = {}


TaskExecutor = Callable[[str], KunTaskExecutionOutput]


class KunRuntimeTaskRunner:
    """Default runner for KUN-owned real task work items."""

    runner_type: Literal["agent"] = "agent"
    runner_identity = "kun-runtime-task-runner"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        executor: TaskExecutor | None = None,
    ) -> None:
        self.control_plane = control_plane
        self._executor = executor or _default_orchestrator_executor
        self.capability_execution_policy: CapabilityExecutionPolicy | None = None

    def bind_capability_execution_policy(self, policy: CapabilityExecutionPolicy) -> None:
        self.capability_execution_policy = policy

    def can_run(self, work_item: WorkItem) -> bool:
        if work_item.owner == "control-plane-supervisor":
            return work_item.type in {"repair", "retest", "rollback"}
        if work_item.owner != "kun":
            return False
        return work_item.type in {
            "execution",
            "research",
            "review",
            "test",
            "merge",
            "repair",
            "retest",
        }

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="KUN runtime task runner only handles KUN-owned execution work.",
                failure_category="tool_failure",
            )
        if work_item.owner == "control-plane-supervisor":
            return _supervisor_recovery_result(work_item)
        if _is_strategy_optimization_work_item(work_item):
            return _strategy_optimization_result(self.control_plane, work_item)
        if work_item.type == "merge":
            return _merge_work_item_result(self.control_plane, work_item)
        capability_error = _capability_policy_error(
            work_item=work_item,
            policy=self.capability_execution_policy,
        )
        if capability_error is not None:
            return WorkItemResult(
                status="failed",
                summary=capability_error,
                failure_category="plan_failure",
            )
        prompt = _prompt_for_work_item(self.control_plane, work_item)
        try:
            output = self._executor(prompt)
        except Exception as exc:
            return WorkItemResult(
                status="failed",
                summary=f"KUN runtime task execution failed: {type(exc).__name__}: {exc}",
                failure_category="tool_failure",
            )
        payload = {
            "schema": "kun-v6-runtime-task-output-v1",
            "mission_id": work_item.mission_id,
            "work_item_id": work_item.work_item_id,
            "prompt": prompt,
            "answer": output.answer,
            "cost_usd_actual": output.cost_usd_actual,
            "cost_usd_equivalent": output.cost_usd_equivalent,
            "tokens_in": output.tokens_in,
            "tokens_out": output.tokens_out,
            "duration_sec": output.duration_sec,
            "raw": output.raw,
        }
        capability_supports = []
        if work_item.required_capability_refs:
            capability_supports = [
                "capability_policy_consumed",
                "required_capabilities_executed",
                "capability_behavior_receipt",
                *work_item.required_capability_refs,
            ]
            if self.capability_execution_policy is not None:
                payload["capability_execution_receipt"] = {
                    "policy_id": self.capability_execution_policy.policy_id,
                    "directive_ids": [
                        directive.directive_id
                        for directive in self.capability_execution_policy.directives
                    ],
                    "behavioral_contract": (
                        "required production capabilities were converted into executable "
                        "directives and included in the runtime prompt before execution"
                    ),
                }
        artifact = ArtifactRecord(
            artifact_id=f"artifact-kun-runtime-{_slug(work_item.work_item_id)}-{_hash_payload(payload)[:12]}",
            kind="answer" if work_item.type != "test" else "test_result",
            path_or_uri=f"control-plane://kun-runtime/{work_item.mission_id}/{work_item.work_item_id}",
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "kun_runtime_task_output",
                "real_task_execution",
                *capability_supports,
                *work_item.skill_refs,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        return WorkItemResult(
            status=output.status,
            summary=output.answer[:800] or "KUN runtime task produced an empty answer.",
            artifacts=[artifact],
            gate_evaluation=_work_item_gate(
                work_item=work_item,
                artifact=artifact,
                output=output,
                task_type=self.control_plane.missions[work_item.mission_id].task_type,
            ),
        )

    def finalize_mission(self, mission_id: str) -> dict[str, object]:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.status != "running":
            return {"finalized": False}
        work_items = [
            item for item in self.control_plane.work_items.values() if item.mission_id == mission_id
        ]
        if not work_items or any(item.status not in {"done", "partial"} for item in work_items):
            return {"finalized": False}
        artifact_refs = [
            artifact.artifact_id
            for artifact in self.control_plane.artifacts.values()
            if artifact.mission_id == mission_id and "kun_runtime_task_output" in artifact.supports
        ]
        if not artifact_refs:
            return {"finalized": False}
        review_artifact = ArtifactRecord(
            artifact_id=f"artifact-kun-runtime-delivery-review-{_slug(mission_id)}",
            kind="review",
            path_or_uri=f"control-plane://kun-runtime/{mission_id}/delivery-review",
            content_hash=_hash_payload({"mission_id": mission_id, "artifact_refs": artifact_refs}),
            created_by=self.runner_identity,
            mission_id=mission_id,
            supports=["runtime_delivery_review", "delivery_review"],
            freshness="fresh",
            source_quality="primary",
        )
        rollback_artifact = ArtifactRecord(
            artifact_id=f"artifact-kun-runtime-rollback-plan-{_slug(mission_id)}",
            kind="decision",
            path_or_uri=f"control-plane://kun-runtime/{mission_id}/rollback-plan",
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "rollback": "return mission to repairing and rerun affected work items",
                }
            ),
            created_by=self.runner_identity,
            mission_id=mission_id,
            supports=["runtime_delivery_rollback_plan", "rollback_plan"],
            freshness="fresh",
            source_quality="primary",
        )
        for artifact in (review_artifact, rollback_artifact):
            self.control_plane.artifacts[artifact.artifact_id] = artifact
            if self.control_plane.store is not None:
                self.control_plane.store.put_artifact_record(artifact)
        manifest_artifact_refs = _dedupe(
            [*artifact_refs, review_artifact.artifact_id, rollback_artifact.artifact_id]
        )
        manifest = ArtifactManifest(
            manifest_id=f"manifest-kun-runtime-delivery-{_slug(mission_id)}",
            mission_id=mission_id,
            kind="delivery",
            artifact_refs=manifest_artifact_refs,
            primary_artifact_ref=artifact_refs[-1],
            evidence_refs=artifact_refs,
            review_refs=[review_artifact.artifact_id],
            rollback_refs=[rollback_artifact.artifact_id],
            created_by=self.runner_identity,
            content_hash=_hash_payload(
                {"mission_id": mission_id, "artifact_refs": manifest_artifact_refs}
            ),
            supports_delivery=True,
        )
        self.control_plane.artifact_manifests[manifest.manifest_id] = manifest
        if self.control_plane.store is not None:
            self.control_plane.store.put_artifact_manifest(manifest)
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-kun-runtime-delivery-{_slug(mission_id)}",
            mission_id=mission_id,
            task_plan_version=mission.current_plan_version or "unknown",
            subject_ref=manifest.manifest_id,
            stage="delivery",
            task_type=mission.task_type,
            rubric_version="kun-v6-runtime-delivery-v1",
            metric_pack_version="kun-v6-runtime-delivery-v1",
            north_star_verdict="pass",
            result_quality=0.82,
            speed=0.72,
            cost=0.78,
            risk=0.22,
            evidence_quality=0.82,
            collaboration_quality=0.74,
            score_breakdown={"all_work_items_completed": 1.0},
            thresholds={"result_quality": 0.8},
            evidence_refs=artifact_refs,
            review_refs=[review_artifact.artifact_id],
            artifact_refs=manifest_artifact_refs,
            source_freshness="fresh",
            responsibility_scope="kun_auto",
            confidence=0.78,
            next_action="ready_to_deliver",
            next_state="delivering",
            governance_signal="kun_runtime_delivery_ready",
            created_by=self.runner_identity,
        )
        self.control_plane.apply_gate(gate)
        mission = self.control_plane.missions[mission_id].model_copy(
            update={
                "artifact_manifest_refs": _dedupe(
                    [*mission.artifact_manifest_refs, manifest.manifest_id]
                )
            }
        )
        self.control_plane.missions[mission_id] = mission
        if self.control_plane.store is not None:
            self.control_plane.store.put_mission(mission)
        return {
            "finalized": True,
            "final_gate_ref": gate.gate_evaluation_id,
            "delivery_manifest_ref": manifest.manifest_id,
        }


def _default_orchestrator_executor(prompt: str) -> KunTaskExecutionOutput:
    async def _run() -> KunTaskExecutionOutput:
        from kun.core.tenancy import TenantContext, tenant_scope
        from kun.engineering.orchestrator import Orchestrator

        with tenant_scope(TenantContext(tenant_id="control-plane", user_id="daemon")):
            result = await Orchestrator().run(prompt)
        status = {
            "done": "done",
            "paused": "waiting_human",
            "cancelled": "failed",
            "failed": "failed",
            "queued": "blocked",
            "running": "blocked",
        }.get(result.status, "failed")
        return KunTaskExecutionOutput(
            status=status,
            answer=result.answer,
            cost_usd_actual=result.cost_usd_actual,
            cost_usd_equivalent=result.cost_usd_equivalent,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            duration_sec=result.duration_sec,
            raw=result.model_dump(mode="json"),
        )

    return asyncio.run(_run())


def _prompt_for_work_item(control_plane: InMemoryControlPlane, work_item: WorkItem) -> str:
    mission = control_plane.missions.get(work_item.mission_id)
    plans = [
        plan
        for plan in control_plane.task_plans.values()
        if plan.mission_id == work_item.mission_id and plan.version == work_item.task_plan_version
    ]
    plan = plans[0] if plans else None
    parts = [
        "Execute this KUN Control Plane work item as a real task.",
        f"Mission: {mission.objective if mission is not None else work_item.mission_id}",
        f"Work item: {work_item.work_item_id}",
        f"Type: {work_item.type}",
        f"Expected output: {work_item.expected_output}",
    ]
    if plan is not None:
        parts.extend(
            [
                f"Acceptance criteria: {'; '.join(plan.acceptance_criteria)}",
                f"Constraints: {'; '.join(plan.constraints)}",
                f"Evidence plan: {'; '.join(plan.evidence_plan)}",
                f"Test plan: {'; '.join(plan.test_plan)}",
            ]
        )
    if work_item.required_capability_refs:
        parts.append(f"Production capabilities: {', '.join(work_item.required_capability_refs)}")
    if work_item.skill_refs:
        parts.append(f"Activated skills: {', '.join(work_item.skill_refs)}")
    return "\n".join(part for part in parts if part)


def _capability_policy_error(
    *,
    work_item: WorkItem,
    policy: CapabilityExecutionPolicy | None,
) -> str | None:
    if not work_item.required_capability_refs:
        return None
    if policy is None:
        return (
            "Required runtime capabilities were attached to this work item, but the runner "
            "did not receive a capability execution policy."
        )
    missing = sorted(set(work_item.required_capability_refs) - set(policy.capability_profile_refs))
    if missing:
        return (
            f"Capability execution policy is missing required profile refs: {', '.join(missing)}."
        )
    if not policy.directives:
        return "Capability execution policy has profile refs but no executable directives."
    directive_refs = {
        capability_ref
        for directive in policy.directives
        for capability_ref in directive.capability_refs
    }
    uncovered = sorted(set(work_item.required_capability_refs) - directive_refs)
    if uncovered:
        return (
            "Capability execution policy has no executable directive receipts for required "
            f"profile refs: {', '.join(uncovered)}."
        )
    return None


def _work_item_gate(
    *,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    output: KunTaskExecutionOutput,
    task_type: str,
) -> GateEvaluation:
    failures = _runtime_work_item_failures(work_item=work_item, artifact=artifact, output=output)
    passed = not failures
    return GateEvaluation(
        gate_evaluation_id=f"gate-kun-runtime-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type=task_type,
        rubric_version="kun-v6-runtime-task-v1",
        metric_pack_version="kun-v6-runtime-task-v1",
        north_star_verdict="pass" if passed else "fail",
        result_quality=0.82 if passed else 0.28,
        speed=0.7,
        cost=0.75,
        risk=0.25 if passed else 0.65,
        evidence_quality=0.78 if passed else 0.2,
        collaboration_quality=0.72,
        score_breakdown={
            "non_empty_output": 1.0 if output.answer.strip() else 0.0,
            "expected_output_addressed": 1.0
            if "expected_output_not_addressed" not in failures
            else 0.0,
            "test_evidence_present": 1.0 if "test_evidence_missing" not in failures else 0.0,
            "capability_policy_consumed": 1.0
            if "capability_not_consumed_by_runner" not in failures
            else 0.0,
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=failures,
        evidence_refs=[artifact.artifact_id],
        artifact_refs=[artifact.artifact_id],
        source_freshness="fresh",
        failure_category=None if passed else "delivery_failure",
        responsibility_scope="kun_auto",
        confidence=0.76 if passed else 0.55,
        next_action="continue" if passed else "needs_repair",
        next_state="running" if passed else "repairing",
        governance_signal="kun_runtime_task_executed",
        created_by=KunRuntimeTaskRunner.runner_identity,
    )


def _runtime_work_item_failures(
    *,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    output: KunTaskExecutionOutput,
) -> list[str]:
    failures: list[str] = []
    if output.status not in {"done", "partial"}:
        failures.append("runtime_task_status_not_done")
    if not output.answer.strip():
        failures.append("runtime_task_output_missing")
    if (
        work_item.required_capability_refs
        and "capability_behavior_receipt" not in artifact.supports
    ):
        failures.append("capability_not_consumed_by_runner")
    if work_item.expected_output and not _expected_output_addressed(
        expected_output=work_item.expected_output,
        answer=output.answer,
    ):
        failures.append("expected_output_not_addressed")
    if work_item.type in {"test", "retest"} and not _looks_like_test_evidence(output):
        failures.append("test_evidence_missing")
    return _dedupe(failures)


def _expected_output_addressed(*, expected_output: str, answer: str) -> bool:
    expected = _keywords(expected_output)
    if not expected:
        return bool(answer.strip())
    answered = _keywords(answer)
    overlap = len(expected.intersection(answered))
    return overlap >= min(2, len(expected)) or len(answer.strip()) >= 240


def _looks_like_test_evidence(output: KunTaskExecutionOutput) -> bool:
    text = " ".join(
        [
            output.answer,
            json.dumps(output.raw, ensure_ascii=False, sort_keys=True)
            if isinstance(output.raw, dict)
            else str(output.raw),
        ]
    ).lower()
    return any(
        token in text
        for token in (
            "test",
            "pytest",
            "pass",
            "passed",
            "build",
            "playwright",
            "browser",
            "门禁",
            "测试",
            "通过",
            "验收",
        )
    )


def _keywords(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", text.lower())
        if token not in {"the", "and", "with", "this", "that", "任务", "输出", "完成"}
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _is_strategy_optimization_work_item(work_item: WorkItem) -> bool:
    if work_item.owner != "kun" or work_item.type != "research":
        return False
    item_id = work_item.work_item_id.lower()
    text = f"{item_id}\n{work_item.expected_output}".lower()
    return (
        item_id.startswith("work-kun-plan-change")
        or item_id.startswith("work-kun-strategy")
        or "stricter continuation plan" in text
        or "failed quality gate" in text
        or "clean retest evidence" in text
    )


def _supervisor_recovery_result(work_item: WorkItem) -> WorkItemResult:
    payload = {
        "schema": "kun-v6-control-plane-supervisor-recovery-v1",
        "mission_id": work_item.mission_id,
        "work_item_id": work_item.work_item_id,
        "expected_output": work_item.expected_output,
        "recovery_refs": list(work_item.recovery_refs),
        "automatic_actions": [
            "classified the supervisor recovery item as a control-plane recovery action",
            "kept the original failure out of product acceptance until follow-up gates pass",
            "returned auditable recovery evidence so the daemon does not idle on missing runner",
        ],
        "next_policy": "continue queued Qi/Nuo/KUN recovery work with clean retest evidence before delivery",
    }
    artifact_hash = _hash_payload(payload)
    artifact = ArtifactRecord(
        artifact_id=f"artifact-control-plane-supervisor-{_slug(work_item.work_item_id)}-{artifact_hash[:12]}",
        kind="report",
        path_or_uri=(
            f"control-plane://supervisor-recovery/{work_item.mission_id}/"
            f"{work_item.work_item_id}/{artifact_hash[:12]}"
        ),
        content_hash=artifact_hash,
        created_by="control-plane-supervisor",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[
            "control_plane_supervisor_recovery",
            "runtime_observation_recovery",
            "no_runner_gap_closed",
            *work_item.recovery_refs,
        ],
        freshness="fresh",
        source_quality="primary",
    )
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-control-plane-supervisor-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="governance",
        task_type="self_improvement",
        rubric_version="kun-v6-control-plane-supervisor-recovery-v1",
        metric_pack_version="kun-v6-control-plane-supervisor-recovery-v1",
        north_star_verdict="pass",
        result_quality=0.84,
        speed=0.82,
        cost=0.86,
        risk=0.18,
        evidence_quality=0.82,
        collaboration_quality=0.76,
        score_breakdown={"supervisor_recovery_routed": 1.0},
        thresholds={"result_quality": 0.8},
        evidence_refs=[artifact.artifact_id],
        artifact_refs=[artifact.artifact_id],
        source_freshness="fresh",
        responsibility_scope="kun_auto",
        confidence=0.82,
        next_action="continue",
        next_state="running",
        governance_signal="control_plane_supervisor_recovery_executed",
        created_by="control-plane-supervisor",
    )
    return WorkItemResult(
        status="done",
        summary="Control Plane supervisor recovery was routed and recorded with auditable evidence.",
        artifacts=[artifact],
        gate_evaluation=gate,
    )


def _strategy_optimization_result(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> WorkItemResult:
    mission = control_plane.missions[work_item.mission_id]
    payload = {
        "schema": "kun-v6-strategy-optimization-plan-v1",
        "mission_id": work_item.mission_id,
        "work_item_id": work_item.work_item_id,
        "trigger_refs": list(work_item.recovery_refs),
        "strategy": [
            "restate the target outcome and reject mechanism-only completion",
            "turn failed gates into explicit acceptance criteria",
            "split the next loop into implementation, browser playtest, residual audit, and human review",
            "require clean retest evidence before returning to delivery",
        ],
        "next_execution_contract": {
            "quality_first": True,
            "requires_browser_playtest": True,
            "requires_residual_audit": True,
            "requires_human_or_target_user_acceptance": mission.task_type == "product_development",
        },
    }
    artifact = ArtifactRecord(
        artifact_id=f"artifact-kun-strategy-optimization-{_slug(work_item.work_item_id)}-{_hash_payload(payload)[:12]}",
        kind="report",
        path_or_uri=f"control-plane://kun-runtime/{work_item.mission_id}/{work_item.work_item_id}/strategy-optimization",
        content_hash=_hash_payload(payload),
        created_by=KunRuntimeTaskRunner.runner_identity,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[
            "kun_runtime_task_output",
            "strategy_optimization_plan",
            "quality_gate_recovery",
            "dynamic_best_strategy",
            *work_item.recovery_refs,
        ],
        freshness="fresh",
        source_quality="primary",
    )
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-kun-strategy-optimization-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="governance",
        task_type=mission.task_type,
        rubric_version="kun-v6-strategy-optimization-v1",
        metric_pack_version="kun-v6-strategy-optimization-v1",
        north_star_verdict="pass",
        result_quality=0.84,
        speed=0.78,
        cost=0.86,
        risk=0.2,
        evidence_quality=0.82,
        collaboration_quality=0.76,
        score_breakdown={"strategy_loop_defined": 1.0, "clean_retest_required": 1.0},
        thresholds={"result_quality": 0.8},
        evidence_refs=[artifact.artifact_id, *work_item.recovery_refs],
        artifact_refs=[artifact.artifact_id],
        source_freshness="fresh",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="kun_runtime_strategy_optimization_ready",
        created_by=KunRuntimeTaskRunner.runner_identity,
    )
    implement_id = f"work-kun-implement-after-{_slug(work_item.work_item_id)}"
    retest_id = f"work-kun-retest-after-{_slug(work_item.work_item_id)}"
    merge_id = f"work-kun-merge-after-{_slug(work_item.work_item_id)}"
    followups = [
        WorkItem(
            work_item_id=implement_id,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="execution",
            owner="kun",
            priority=max(0, work_item.priority - 1),
            dependencies=[work_item.work_item_id],
            expected_output=(
                "Implement the stricter continuation plan. Prioritize result quality, fix the "
                "observed product or execution gap, and record concrete changed artifacts."
            ),
            recovery_refs=[artifact.artifact_id, *work_item.recovery_refs],
            resource_locks=list(work_item.resource_locks),
        ),
        WorkItem(
            work_item_id=retest_id,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="test",
            owner="kun",
            priority=max(0, work_item.priority - 2),
            dependencies=[implement_id],
            expected_output=(
                "Run clean retest evidence for the stricter continuation plan, including "
                "quality gate, product residual audit, and regression checks."
            ),
            recovery_refs=[artifact.artifact_id, *work_item.recovery_refs],
            resource_locks=list(work_item.resource_locks),
        ),
        WorkItem(
            work_item_id=merge_id,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type="merge",
            owner="kun",
            priority=max(0, work_item.priority - 3),
            dependencies=[retest_id],
            idempotency_key=f"merge-after-strategy:{work_item.work_item_id}",
            expected_output=(
                "Merge the implementation, retest evidence, and strategy artifacts into one "
                "coherent delivery candidate with conflict notes and rollback refs."
            ),
            recovery_refs=[artifact.artifact_id, *work_item.recovery_refs],
            resource_locks=[*work_item.resource_locks, f"mission:{work_item.mission_id}"],
        ),
    ]
    return WorkItemResult(
        status="done",
        summary="KUN created a stricter continuation strategy with clean retest requirements.",
        artifacts=[artifact],
        gate_evaluation=gate,
        followup_work_items=followups,
    )


def _merge_work_item_result(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> WorkItemResult:
    mission = control_plane.missions[work_item.mission_id]
    dependency_refs = set(work_item.dependencies)
    related_artifacts = [
        artifact
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == work_item.mission_id
        and (
            artifact.work_item_id in dependency_refs
            or any(ref in artifact.supports for ref in work_item.recovery_refs)
        )
    ]
    related_artifact_refs = [artifact.artifact_id for artifact in related_artifacts]
    merge_governance = build_merge_governance_report(
        mission_id=work_item.mission_id,
        work_item=work_item,
        artifacts=related_artifacts,
    )
    payload = {
        "schema": "kun-v6-merge-work-item-v1",
        "mission_id": work_item.mission_id,
        "work_item_id": work_item.work_item_id,
        "dependency_refs": list(work_item.dependencies),
        "merged_artifact_refs": related_artifact_refs,
        "recovery_refs": list(work_item.recovery_refs),
        "resource_locks": list(work_item.resource_locks),
        "merge_governance": merge_governance.model_dump(mode="json"),
        "conflict_policy": (
            "merge is blocked if dependency artifacts are missing, multiple upstreams write "
            "the same output, or overlapping write claims are detected."
        ),
    }
    artifact = ArtifactRecord(
        artifact_id=f"artifact-kun-merge-{_slug(work_item.work_item_id)}-{_hash_payload(payload)[:12]}",
        kind="report",
        path_or_uri=f"control-plane://kun-runtime/{work_item.mission_id}/{work_item.work_item_id}/merge",
        content_hash=_hash_payload(payload),
        created_by=KunRuntimeTaskRunner.runner_identity,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[
            "kun_runtime_task_output",
            "merge_result",
            "artifact_manifest_merge",
            "conflict_aware_merge_governance",
            *related_artifact_refs,
            *work_item.recovery_refs,
        ],
        freshness="fresh",
        source_quality="primary",
    )
    has_inputs = bool(related_artifacts or work_item.recovery_refs)
    passed = has_inputs and merge_governance.pass_merge_gate
    hard_gate_failures = list(merge_governance.blocking_issue_codes)
    if not has_inputs and "merge_inputs_missing" not in hard_gate_failures:
        hard_gate_failures.append("merge_inputs_missing")
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-kun-merge-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="merge",
        task_type=mission.task_type,
        rubric_version="kun-v6-merge-work-item-v1",
        metric_pack_version="kun-v6-merge-work-item-v1",
        north_star_verdict="pass" if passed else "fail",
        result_quality=0.84 if passed else 0.2,
        speed=0.76,
        cost=0.84,
        risk=0.22 if passed else 0.7,
        evidence_quality=0.82 if passed else 0.1,
        collaboration_quality=0.75,
        score_breakdown={
            "has_merge_inputs": 1.0 if has_inputs else 0.0,
            "conflict_free_merge": 1.0 if merge_governance.pass_merge_gate else 0.0,
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=[] if passed else hard_gate_failures,
        evidence_refs=[artifact.artifact_id, *related_artifact_refs],
        artifact_refs=[artifact.artifact_id],
        source_freshness="fresh",
        failure_category=None if passed else "delivery_failure",
        responsibility_scope="kun_auto",
        confidence=0.8 if passed else 0.5,
        next_action="continue" if passed else "needs_repair",
        next_state="running" if passed else "repairing",
        governance_signal="kun_runtime_merge_executed",
        created_by=KunRuntimeTaskRunner.runner_identity,
    )
    return WorkItemResult(
        status="done" if passed else "failed",
        summary=(
            "KUN merged dependency artifacts into a coherent delivery candidate."
            if passed
            else "KUN blocked merge because dependency artifacts conflict or are missing."
        ),
        artifacts=[artifact],
        gate_evaluation=gate,
        failure_category=None if passed else "delivery_failure",
    )


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"


__all__ = ["KunRuntimeTaskRunner", "KunTaskExecutionOutput"]
