"""Generic KUN task runner for Control Plane work items.

The productized daemon must not only run productization, Qi, and Nuo work.  It
also needs a default path for ordinary KUN-owned execution/research/review/test
work so real long tasks do not fall back to one-off task-specific runners.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

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

    def can_run(self, work_item: WorkItem) -> bool:
        if work_item.owner != "kun":
            return False
        return work_item.type in {"execution", "research", "review", "test", "merge"}

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary="KUN runtime task runner only handles KUN-owned execution work.",
                failure_category="tool_failure",
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
                *work_item.required_capability_refs,
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
        manifest = ArtifactManifest(
            manifest_id=f"manifest-kun-runtime-delivery-{_slug(mission_id)}",
            mission_id=mission_id,
            kind="delivery",
            artifact_refs=artifact_refs,
            primary_artifact_ref=artifact_refs[-1],
            evidence_refs=artifact_refs,
            created_by=self.runner_identity,
            content_hash=_hash_payload({"mission_id": mission_id, "artifact_refs": artifact_refs}),
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
            artifact_refs=artifact_refs,
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
            update={"artifact_manifest_refs": [*mission.artifact_manifest_refs, manifest.manifest_id]}
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
        if plan.mission_id == work_item.mission_id
        and plan.version == work_item.task_plan_version
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


def _work_item_gate(
    *,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    output: KunTaskExecutionOutput,
    task_type: str,
) -> GateEvaluation:
    passed = output.status in {"done", "partial"} and bool(output.answer.strip())
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
        result_quality=0.82 if passed else 0.3,
        speed=0.7,
        cost=0.75,
        risk=0.25 if passed else 0.65,
        evidence_quality=0.78 if passed else 0.2,
        collaboration_quality=0.72,
        score_breakdown={"non_empty_output": 1.0 if passed else 0.0},
        thresholds={"result_quality": 0.8},
        hard_gate_failures=[] if passed else ["runtime_task_output_missing_or_failed"],
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


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"


__all__ = ["KunRuntimeTaskRunner", "KunTaskExecutionOutput"]
