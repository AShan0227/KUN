"""Generic KUN task runner for Control Plane work items.

The productized daemon must not only run productization, Qi, and Nuo work.  It
also needs a default path for ordinary KUN-owned execution/research/review/test
work so real long tasks do not fall back to one-off task-specific runners.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.runtime import InMemoryControlPlane, WorkItemResult
from kun.control_plane.v6 import ArtifactManifest, ArtifactRecord, GateEvaluation, WorkItem
from kun.control_plane.work_item_governance import build_merge_governance_report


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
RuntimeFallbackExecutor = Callable[
    [str, WorkItem, InMemoryControlPlane, str], KunTaskExecutionOutput
]


class KunRuntimeTaskRunner:
    """Default runner for KUN-owned real task work items."""

    runner_type: Literal["agent"] = "agent"
    runner_identity = "kun-runtime-task-runner"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        executor: TaskExecutor | None = None,
        fallback_executor: RuntimeFallbackExecutor | None = None,
    ) -> None:
        self.control_plane = control_plane
        self._executor = executor or _default_orchestrator_executor
        self._uses_default_executor = executor is None
        self._environment_fallback_enabled = (
            self._uses_default_executor or fallback_executor is not None
        )
        self._fallback_executor = fallback_executor or _codex_mcp_direct_runtime_executor
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
        fallback_summary: str | None = None
        try:
            output = self._executor(prompt)
        except Exception as exc:
            summary = f"KUN runtime task execution failed: {type(exc).__name__}: {exc}"
            failure_category = _executor_exception_failure_category(summary)
            if self._environment_fallback_enabled:
                if failure_category == "environment_failure":
                    try:
                        fallback_summary = summary
                        output = self._fallback_executor(
                            _direct_runtime_fallback_prompt(
                                control_plane=self.control_plane,
                                work_item=work_item,
                                prompt=prompt,
                                primary_failure_summary=summary,
                            ),
                            work_item,
                            self.control_plane,
                            summary,
                        )
                    except Exception as fallback_exc:
                        fallback_summary = (
                            f"{summary}; direct fallback failed: "
                            f"{type(fallback_exc).__name__}: {fallback_exc}"
                        )
                        return WorkItemResult(
                            status="failed",
                            summary=fallback_summary,
                            failure_category=_executor_exception_failure_category(fallback_summary),
                        )
                else:
                    return WorkItemResult(
                        status="failed",
                        summary=summary,
                        failure_category=failure_category,
                    )
            else:
                return WorkItemResult(
                    status="failed",
                    summary=summary,
                    failure_category=failure_category,
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
        if fallback_summary is not None:
            payload["primary_failure_summary"] = fallback_summary
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
        artifact_kind = _artifact_kind_for_work_item(work_item)
        artifact_supports = _artifact_supports_for_work_item(
            work_item=work_item,
            artifact_kind=artifact_kind,
            capability_supports=capability_supports,
        )
        if output.raw.get("runtime_fallback"):
            artifact_supports = _dedupe(
                [
                    *artifact_supports,
                    "direct_runtime_fallback",
                    "orchestrator_environment_fallback",
                ]
            )
        artifact = ArtifactRecord(
            artifact_id=f"artifact-kun-runtime-{_slug(work_item.work_item_id)}-{_hash_payload(payload)[:12]}",
            kind=artifact_kind,
            path_or_uri=f"control-plane://kun-runtime/{work_item.mission_id}/{work_item.work_item_id}",
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=artifact_supports,
            freshness="fresh",
            source_quality="primary",
        )
        local_evidence_artifacts = _local_evidence_artifacts_for_work_item(
            self.control_plane,
            work_item,
        )
        return WorkItemResult(
            status=output.status,
            summary=output.answer[:800] or "KUN runtime task produced an empty answer.",
            artifacts=[artifact, *local_evidence_artifacts],
            gate_evaluation=_work_item_gate(
                work_item=work_item,
                artifact=artifact,
                local_evidence_artifacts=local_evidence_artifacts,
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
        active_work_items = [
            item
            for item in work_items
            if item.status != "cancelled"
            and (
                mission.current_plan_version is None
                or item.task_plan_version == mission.current_plan_version
            )
            and not _is_nonblocking_governance_followup(item)
        ]
        if not active_work_items or any(
            item.status not in {"done", "partial"} for item in active_work_items
        ):
            return {"finalized": False}
        active_work_item_ids = {item.work_item_id for item in active_work_items}
        delivery_artifacts = [
            artifact
            for artifact in self.control_plane.artifacts.values()
            if artifact.mission_id == mission_id
            and artifact.work_item_id in active_work_item_ids
            and _is_delivery_evidence_artifact(artifact)
        ]
        artifact_refs = [artifact.artifact_id for artifact in delivery_artifacts]
        task_output_refs = [
            artifact.artifact_id
            for artifact in delivery_artifacts
            if "kun_runtime_task_output" in artifact.supports
        ]
        test_refs = [
            artifact.artifact_id
            for artifact in delivery_artifacts
            if artifact.kind == "test_result"
            or "runtime_test_evidence" in artifact.supports
            or "test_result" in artifact.supports
        ]
        local_review_refs = [
            artifact.artifact_id
            for artifact in delivery_artifacts
            if artifact.kind == "review"
            or "runtime_review" in artifact.supports
            or "review" in artifact.supports
        ]
        if not artifact_refs or not task_output_refs:
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
            primary_artifact_ref=task_output_refs[-1],
            test_refs=test_refs,
            evidence_refs=artifact_refs,
            review_refs=_dedupe([*local_review_refs, review_artifact.artifact_id]),
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
            review_refs=_dedupe([*local_review_refs, review_artifact.artifact_id]),
            artifact_refs=manifest_artifact_refs,
            test_refs=test_refs,
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


def _is_nonblocking_governance_followup(work_item: WorkItem) -> bool:
    if work_item.owner == "mission-director" and work_item.work_item_id.startswith(
        "work-mission-director-"
    ):
        return True
    if work_item.owner != "qi":
        return False
    if work_item.type not in {"governance", "research"}:
        return False
    identifier = f"{work_item.work_item_id} {work_item.idempotency_key or ''}"
    return (
        "runtime-learning" in identifier
        or "runtime-observation:qi:" in identifier
        or "mechanical_acceptance_rework_loop" in identifier
    )


def _is_delivery_evidence_artifact(artifact: ArtifactRecord) -> bool:
    supports = set(artifact.supports)
    if supports & {
        "workspace_snapshot",
        "workspace_checkpoint",
        "runtime_feature_activation",
        "skill_preflight",
    }:
        return False
    return bool(
        supports
        & {
            "kun_runtime_task_output",
            "local_runtime_evidence",
            "runtime_test_evidence",
            "runtime_review",
            "runtime_report",
            "local_output_manifest",
            "merge_result",
        }
        or artifact.kind in {"answer", "evidence", "report", "review", "test_result"}
    )


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


def _codex_mcp_direct_runtime_executor(
    prompt: str,
    work_item: WorkItem,
    control_plane: InMemoryControlPlane,
    primary_failure_summary: str,
) -> KunTaskExecutionOutput:
    """Run a KUN work item directly when the local orchestrator is environment-blocked."""

    from kun.interface.llm import LLMMessage, LLMRequest, TaskProfile
    from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

    if not CodexMcpProvider.available():
        raise RuntimeError("codex MCP direct runtime fallback unavailable: codex CLI not found")

    workspace_path = _workspace_path_for_work_item(control_plane, work_item)
    timeout_sec = _env_int("KUN_RUNTIME_DIRECT_CODEX_TIMEOUT_SEC", 900)
    reasoning_effort = os.getenv("KUN_RUNTIME_DIRECT_CODEX_REASONING", "high")
    sandbox = os.getenv("KUN_RUNTIME_DIRECT_CODEX_SANDBOX", "workspace-write")
    provider = CodexMcpProvider(
        tier="coding",
        run_cwd=str(workspace_path) if workspace_path is not None else None,
        timeout_sec=timeout_sec,
        reasoning_effort=reasoning_effort,
        sandbox=sandbox,
    )
    request = LLMRequest(
        messages=[LLMMessage(role="user", content=prompt)],
        profile=TaskProfile(
            task_type="control_plane_runtime_direct_fallback",
            risk_level="high",
            needs_coding=True,
            needs_reasoning=True,
            max_duration_sec=float(timeout_sec),
            audience="developer",
        ),
        max_tokens=_env_int("KUN_RUNTIME_DIRECT_CODEX_MAX_TOKENS", 4096),
    )

    async def _invoke() -> KunTaskExecutionOutput:
        try:
            response = await provider.invoke(request)
        finally:
            await provider.close()
        answer = response.content.strip()
        return KunTaskExecutionOutput(
            status="done" if answer else "blocked",
            answer=answer
            or "Direct runtime fallback returned an empty response and could not execute the work.",
            cost_usd_actual=response.cost_usd_actual,
            cost_usd_equivalent=response.cost_usd_equivalent,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            duration_sec=response.latency_ms / 1000,
            raw={
                "runtime_fallback": "codex_mcp_direct",
                "primary_failure_summary": primary_failure_summary,
                "workspace_path": str(workspace_path) if workspace_path is not None else None,
                "sandbox": sandbox,
                "model": response.model,
                "provider": response.provider,
            },
        )

    return asyncio.run(_invoke())


def _direct_runtime_fallback_prompt(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    prompt: str,
    primary_failure_summary: str,
) -> str:
    workspace_path = _workspace_path_for_work_item(control_plane, work_item)
    mission = control_plane.missions.get(work_item.mission_id)
    boundaries = [
        f"workspace_ref: {work_item.workspace_ref or 'none'}",
        f"resolved workspace path: {workspace_path or 'none'}",
        f"sandbox_ref: {work_item.sandbox_ref or 'none'}",
        f"resource_locks: {', '.join(work_item.resource_locks) or 'none'}",
    ]
    return "\n".join(
        [
            "The primary KUN orchestrator was environment-blocked before it could execute.",
            f"Primary failure: {primary_failure_summary}",
            "Execute the work item directly in the isolated workspace instead.",
            "Do not commit, push, merge, stop daemons, or touch unrelated workspaces.",
            "If code changes are needed, edit only inside the resolved workspace path.",
            (
                "Create a new unique output directory for this attempt; do not delete, "
                "overwrite, or clean existing outputs, and do not use rm -rf, git clean, "
                "git reset --hard, or git checkout --."
            ),
            "Run the smallest meaningful checks you can, and report concrete files/tests/evidence.",
            "If direct execution is still blocked, state the exact blocker and what needs repair.",
            f"Mission objective: {mission.objective if mission is not None else work_item.mission_id}",
            "Isolation boundaries:",
            *boundaries,
            "",
            "Original Control Plane prompt:",
            prompt,
        ]
    )


def _workspace_path_for_work_item(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> Path | None:
    value = _workspace_path_from_ref(work_item.workspace_ref)
    if value is None:
        mission = control_plane.missions.get(work_item.mission_id)
        contract = (
            control_plane.contracts.get(mission.execution_contract_ref or "")
            if mission is not None
            else None
        )
        value = _find_first_path(
            [
                getattr(contract, "delivery_contract", None),
                getattr(contract, "risk_policy", None),
                getattr(contract, "rollback_policy", None),
            ]
        )
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def _workspace_path_from_ref(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("workspace://"):
        return value.removeprefix("workspace://")
    return value


def _find_first_path(value: Any) -> str | None:
    keys = (
        "workspace_path",
        "project_path",
        "repo_path",
        "target_path",
        "output_dir",
        "delivery_path",
    )
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            found = _find_first_path(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_first_path(item)
            if found:
                return found
    return None


def _local_evidence_artifacts_for_work_item(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> list[ArtifactRecord]:
    workspace_path = _workspace_path_for_work_item(control_plane, work_item)
    if workspace_path is None:
        return []
    roots = _local_output_roots(workspace_path=workspace_path, work_item=work_item)
    fresh_after = _local_evidence_fresh_after_epoch(work_item)
    artifacts: list[ArtifactRecord] = []
    for path in _local_evidence_files(roots, fresh_after=fresh_after):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        content_hash = hashlib.sha256(content).hexdigest()
        try:
            relative = path.relative_to(workspace_path)
        except ValueError:
            relative = path
        artifacts.append(
            ArtifactRecord(
                artifact_id=(
                    f"artifact-kun-runtime-local-evidence-{_slug(work_item.work_item_id)}-"
                    f"{_slug(str(relative))}-{content_hash[:12]}"
                ),
                kind=_local_evidence_kind(path),
                path_or_uri=str(path),
                content_hash=content_hash,
                created_by=KunRuntimeTaskRunner.runner_identity,
                mission_id=work_item.mission_id,
                work_item_id=work_item.work_item_id,
                supports=_local_evidence_supports(path),
                freshness="fresh",
                source_quality="primary",
            )
        )
    return artifacts


def _local_output_roots(*, workspace_path: Path, work_item: WorkItem) -> list[Path]:
    output_parent_dirs = _local_output_parent_dirs(
        workspace_path=workspace_path, work_item=work_item
    )
    if not output_parent_dirs:
        return []
    exact_aliases = {
        _normalize_local_output_token(work_item.work_item_id),
        _normalize_local_output_token(_slug(work_item.work_item_id)),
    }
    exact_roots: list[Path] = []
    for output_parent_dir in output_parent_dirs:
        exact_roots.extend(
            _matching_local_output_roots(outputs_dir=output_parent_dir, aliases=exact_aliases)
        )
    if exact_roots:
        return _current_plan_local_output_roots(
            roots=exact_roots,
            outputs_dir=workspace_path,
            work_item=work_item,
        )
    aliases = _local_output_aliases(work_item)
    roots: list[Path] = []
    preferred_roots: list[Path] = []
    for output_parent_dir in output_parent_dirs:
        matched = _matching_local_output_roots(outputs_dir=output_parent_dir, aliases=aliases)
        roots.extend(matched)
        if output_parent_dir.name in {"k_output", "k-output", "kun_output", "kun-output"}:
            preferred_roots.extend(matched)
    if preferred_roots and not _requires_current_attempt_local_evidence(work_item):
        roots = preferred_roots
    return _current_plan_local_output_roots(
        roots=roots,
        outputs_dir=workspace_path,
        work_item=work_item,
    )


def _local_output_parent_dirs(*, workspace_path: Path, work_item: WorkItem) -> list[Path]:
    locked_output_dirs: list[Path] = []
    for lock in work_item.resource_locks:
        if not lock.startswith(("output:", "output_dir:")):
            continue
        output_dir = Path(lock.split(":", 1)[1]).expanduser()
        if _path_is_within_workspace(output_dir, workspace_path):
            locked_output_dirs.append(output_dir)
    if locked_output_dirs:
        return _dedupe_paths(path for path in locked_output_dirs if path.is_dir())
    candidates = [
        workspace_path / "outputs",
        workspace_path / "adflow_extreme_outputs",
        workspace_path / "adflow-extreme-outputs",
        workspace_path / "k_output",
        workspace_path / "k-output",
        workspace_path / "kun_output",
        workspace_path / "kun-output",
    ]
    return _dedupe_paths(path for path in candidates if path.is_dir())


def _path_is_within_workspace(path: Path, workspace_path: Path) -> bool:
    try:
        resolved = path.resolve()
        workspace_resolved = workspace_path.resolve()
    except OSError:
        return False
    return resolved == workspace_resolved or workspace_resolved in resolved.parents


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _matching_local_output_roots(*, outputs_dir: Path, aliases: set[str]) -> list[Path]:
    roots: list[Path] = []
    if (
        outputs_dir.is_dir()
        and _matches_local_output_alias(outputs_dir.name, aliases)
        and not _is_superseded_local_attempt_root(outputs_dir)
    ):
        roots.append(outputs_dir)
    try:
        direct_children = list(outputs_dir.iterdir())
    except OSError:
        return []
    for child in direct_children:
        if (
            child.is_dir()
            and _matches_local_output_alias(child.name, aliases)
            and not _is_superseded_local_attempt_root(child)
        ):
            roots.append(child)
    if roots:
        return _latest_local_output_roots(roots)
    try:
        nested = outputs_dir.rglob("*")
        for path in nested:
            if (
                path.is_dir()
                and _matches_local_output_alias(path.name, aliases)
                and not _is_superseded_local_attempt_root(path)
            ):
                roots.append(path)
                if len(roots) >= 4:
                    break
    except OSError:
        return roots
    return _latest_local_output_roots(roots)


def _local_output_aliases(work_item: WorkItem) -> set[str]:
    aliases = {
        _normalize_local_output_token(work_item.work_item_id),
        _normalize_local_output_token(_slug(work_item.work_item_id)),
    }
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.expected_output or "",
            work_item.task_plan_version or "",
        ]
    ).lower()
    normalized_text = _normalize_local_output_token(text)
    for match in re.finditer(r"\b\d{2}-[a-z0-9][a-z0-9-]*", normalized_text):
        step = match.group(0).strip("-")
        if len(step) < 6:
            continue
        aliases.add(step)
        aliases.add(step.replace("-and-", "-"))

    if "material-screening" in normalized_text or "screening-and-selection" in normalized_text:
        aliases.update(
            {
                "material-screening",
                "screening-selection",
                "02-material-screening",
                "02-material-screening-selection",
            }
        )
    if "mixed-edit-repair" in normalized_text:
        aliases.update({"mixed-edit-repair", "01-phase1-mixed-edit-repair"})
    if "assembly-and-creator" in normalized_text or "creator-integration" in normalized_text:
        aliases.update(
            {
                "adflow-extreme-attempt",
                "assembly-creator",
                "creator-integration",
                "03-assembly-creator-integration",
                "03-assembly-and-creator-integration",
            }
        )
    if "demo-comparison" in normalized_text or "retest" in normalized_text:
        aliases.update({"demo-comparison", "phase1-demo-comparison", "04-phase1-demo-comparison"})
    if "acceptance-review" in normalized_text:
        aliases.update({"acceptance-review", "05-phase1-acceptance-review"})
    if "transition-generation" in normalized_text or "stage1" in normalized_text:
        aliases.update({"stage1", "transition-generation", "06-stage1-transition-generation"})
    if "visual-regeneration" in normalized_text or "stage2" in normalized_text:
        aliases.update({"stage2", "visual-regeneration", "08-stage2-visual-regeneration"})
    if "advanced-creative" in normalized_text or "stage3" in normalized_text:
        aliases.update({"stage3", "advanced-creative", "10-stage3-advanced-creative-generation"})
    if "final-delivery" in normalized_text:
        aliases.update({"final-delivery", "08-final-delivery", "12-final-delivery"})
    return {alias for alias in aliases if len(alias) >= 6}


def _matches_local_output_alias(name: str, aliases: set[str]) -> bool:
    normalized = _normalize_local_output_token(name)
    return any(alias in normalized for alias in aliases)


def _normalize_local_output_token(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def _latest_local_output_roots(roots: list[Path]) -> list[Path]:
    return sorted(roots, key=_local_output_root_mtime, reverse=True)[:4]


def _current_plan_local_output_roots(
    *,
    roots: list[Path],
    outputs_dir: Path,
    work_item: WorkItem,
) -> list[Path]:
    """Drop stale RainFlow acceptance-rework roots from older plan loops."""

    current_tokens = _current_plan_tokens_for_output(work_item)
    if not current_tokens:
        return roots
    filtered: list[Path] = []
    tokenless: list[Path] = []
    for root in roots:
        try:
            relative = root.relative_to(outputs_dir)
        except ValueError:
            relative = root
        root_tokens = _plan_tokens_from_text(str(relative))
        if root_tokens and root_tokens.isdisjoint(current_tokens):
            continue
        if not root_tokens:
            tokenless.append(root)
            continue
        filtered.append(root)
    if tokenless:
        filtered.extend(_fresh_tokenless_local_output_roots(tokenless))
    return filtered


def _fresh_tokenless_local_output_roots(roots: list[Path]) -> list[Path]:
    """Keep only the current alias-only output batch.

    Some RainFlow workers write directories such as
    ``phase1-mixed-edit-repair-20260524-2358`` without embedding the active
    acceptance-rework token. In a long dogfood mission, older alias-only roots
    can contain stale blockers from previous plan loops, so keep the freshest
    batch instead of mixing historical runs into the current gate.
    """

    if not roots:
        return []
    sorted_roots = sorted(roots, key=_local_output_root_mtime, reverse=True)
    newest_mtime = _local_output_root_mtime(sorted_roots[0])
    fresh_window_seconds = 300.0
    return [
        root
        for root in sorted_roots
        if newest_mtime - _local_output_root_mtime(root) <= fresh_window_seconds
    ]


def _local_output_root_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _current_plan_tokens_for_output(work_item: WorkItem) -> set[str]:
    return _plan_tokens_from_text(
        " ".join(
            [
                work_item.work_item_id,
                work_item.task_plan_version or "",
                work_item.expected_output or "",
            ]
        )
    )


def _plan_tokens_from_text(value: str) -> set[str]:
    normalized = _normalize_local_output_token(value)
    return {
        token
        for token in (
            match.group(0)
            for match in re.finditer(r"(?:acceptance-)?rework-[a-z0-9]{8}", normalized)
        )
        if not token.rsplit("-", 1)[-1].isdigit()
    }


def _local_evidence_files(roots: list[Path], *, fresh_after: float | None = None) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        try:
            candidates = root.rglob("*")
            for path in candidates:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".json", ".md", ".txt"}:
                    continue
                if not _is_local_evidence_filename(path.name):
                    continue
                if _is_superseded_local_attempt_file(path):
                    continue
                if _is_unselected_phase1_package_file(path):
                    continue
                if _is_phase1_report_superseded_by_accepted_package(path, root):
                    continue
                try:
                    if fresh_after is not None and path.stat().st_mtime < fresh_after:
                        continue
                    if path.stat().st_size > 2_000_000:
                        continue
                except OSError:
                    continue
                files.append(path)
                if len(files) >= 64:
                    return files
        except OSError:
            continue
    return files


def _local_evidence_fresh_after_epoch(work_item: WorkItem) -> float | None:
    if not _requires_current_attempt_local_evidence(work_item):
        return None
    if work_item.heartbeat is None:
        return None
    # The daemon stamps heartbeat immediately before runner execution. Allow a
    # tiny clock/filesystem margin, but reject evidence from previous attempts.
    return max(0.0, work_item.heartbeat.timestamp() - 5.0)


def _requires_current_attempt_local_evidence(work_item: WorkItem) -> bool:
    work_id = work_item.work_item_id
    if work_id.startswith(
        (
            "work-kun-implement-after-",
            "work-kun-retest-after-",
            "work-kun-merge-after-",
        )
    ):
        return True
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.type,
            work_item.expected_output or "",
        ]
    ).lower()
    return work_item.type in {"test", "retest"} and any(
        token in text for token in ("rainflow", "adflow", "phase1", "stage1", "seedance")
    )


def _is_local_evidence_filename(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "manifest",
            "gate",
            "review",
            "report",
            "summary",
            "evidence",
            "screening",
            "selection",
            "candidate",
            "qa",
            "test",
            "probe",
        )
    )


def _is_superseded_local_attempt_file(path: Path) -> bool:
    """Ignore failed attempt evidence when a same-work recovery output supersedes it."""

    for parent in path.parents:
        sibling_names = (
            f"{parent.name}-recovered",
            f"{parent.name}_recovered",
            f"{parent.name}-recovery",
            f"{parent.name}_recovery",
        )
        for sibling_name in sibling_names:
            try:
                sibling = parent.with_name(sibling_name)
            except ValueError:
                continue
            if sibling.is_dir():
                return True
    return False


def _is_superseded_local_attempt_root(path: Path) -> bool:
    """Ignore draft output roots when a same-work real/recovery root supersedes them."""

    sibling_names = (
        f"{path.name}-real",
        f"{path.name}_real",
        f"{path.name}-recovered",
        f"{path.name}_recovered",
        f"{path.name}-recovery",
        f"{path.name}_recovery",
    )
    for sibling_name in sibling_names:
        try:
            sibling = path.with_name(sibling_name)
        except ValueError:
            continue
        if sibling.is_dir():
            return True
    return False


def _is_unselected_phase1_package_file(path: Path) -> bool:
    """Skip rejected candidate package files when the manifest selected a different package."""

    for package_root in path.parents:
        if package_root.name != "phase1-improved-demo-packages":
            continue
        try:
            relative = path.relative_to(package_root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) < 2:
            return False
        manifest_path = package_root / "manifest.json"
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        selected_demo_ids = {
            str(package.get("demo_id"))
            for package in manifest.get("packages", [])
            if isinstance(package, dict) and package.get("demo_id")
        }
        if not selected_demo_ids:
            return False
        return parts[0] not in selected_demo_ids
    return False


def _is_phase1_report_superseded_by_accepted_package(path: Path, root: Path) -> bool:
    """Skip stale candidate-level blocker reports once a rendered Phase 1 package passed."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    if "reports" not in parts:
        return False
    if "phase1-improved-demo-packages" in parts:
        return False
    lowered = str(relative).lower()
    if "phase1" not in lowered and "ai-video-stage-gate" not in lowered:
        return False
    return bool(_accepted_phase1_demo_ids(root))


def _accepted_phase1_demo_ids(root: Path) -> set[str]:
    accepted: set[str] = set()
    try:
        manifests = list(root.rglob("phase1-improved-demo-packages/manifest.json"))
    except OSError:
        return accepted
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        packages = manifest.get("packages", [])
        if not isinstance(packages, list):
            continue
        for package in packages:
            if not isinstance(package, dict):
                continue
            demo_id = package.get("demo_id")
            if not demo_id:
                continue
            blocker_codes = package.get("blocker_codes", [])
            if blocker_codes is None:
                blocker_codes = []
            if (
                package.get("delivery_status") == "rendered_review_ready_demo"
                and package.get("human_simulation_accepted") is True
                and package.get("phase1_comparison_accepted") is True
                and not blocker_codes
                and (manifest_path.parent / str(demo_id)).is_dir()
            ):
                accepted.add(str(demo_id))
    return accepted


def _local_evidence_kind(path: Path) -> str:
    lowered = path.name.lower()
    if "review" in lowered:
        return "review"
    if "test" in lowered or "qa" in lowered or "gate" in lowered:
        return "test_result"
    if "report" in lowered or "summary" in lowered or path.suffix.lower() == ".md":
        return "report"
    return "evidence"


def _local_evidence_supports(path: Path) -> list[str]:
    lowered = path.name.lower()
    supports = ["local_runtime_evidence", "workspace_output_trace"]
    if "manifest" in lowered:
        supports.append("local_output_manifest")
    if "review" in lowered:
        supports.extend(["runtime_review", "review"])
    if "report" in lowered or "summary" in lowered:
        supports.extend(["runtime_report", "report"])
    if "test" in lowered or "qa" in lowered or "gate" in lowered:
        supports.extend(["runtime_test_evidence", "test_result"])
    return _dedupe(supports)


def _local_evidence_blocking_failures(
    artifacts: list[ArtifactRecord],
    *,
    work_item: WorkItem,
) -> list[str]:
    if not _enforces_local_delivery_blocking_evidence(work_item):
        return []
    failures: list[str] = []
    for artifact in artifacts:
        path = Path(artifact.path_or_uri)
        path_name = path.name.lower()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if (
            "reject_weak_ad_grade_output" in text
            or '"human_simulation_accepted": false' in text
            or "decision: rejected" in text
        ):
            failures.append("local_human_simulation_rejected")
        if (
            "blocked_not_deliverable_demo" in text
            or "no client-deliverable mp4" in text
            or "rendered_review_ready_demo_missing" in text
        ):
            failures.append("local_deliverable_demo_blocked")
        if (
            '"phase1_comparison_accepted": false' in text
            or 'phase1_comparison_status": "blocked' in text
        ):
            failures.append("local_phase1_comparison_not_accepted")
        if "ai-video gate" in text and "remains blocked" in text:
            failures.append("local_ai_stage_gate_blocked")
        failures.extend(_provider_materialization_failures(text=text, work_item=work_item))
        if (
            "browser" in path_name
            and (
                '"status": "blocked' in text
                or "blocked_by_environment" in text
                or "codex_in_app_browser_unavailable" in text
                or "browser is not available" in text
                or "no iab browser session" in text
            )
            and not _browser_gate_has_local_fallback_evidence(text)
        ):
            failures.append("local_browser_player_gate_blocked")
    return _dedupe(failures)


def _provider_materialization_failures(*, text: str, work_item: WorkItem) -> list[str]:
    if not _expects_provider_video_materialization(work_item):
        return []
    failures: list[str] = []
    provider_network_tokens = (
        "provider_network_failure",
        "blocked_provider_network_failure",
        "dns/name resolution failed",
        "name resolution failed",
        "nodename nor servname provided",
        "temporary failure in name resolution",
    )
    if any(token in text for token in provider_network_tokens):
        failures.append("local_provider_network_blocked")
    provider_blocked_tokens = (
        "planning_ready_provider_blocked",
        "no_insertable_bridge_evidence",
        "output_missing",
        '"ready_for_flow": false',
        "ready_for_flow: `false`",
        "ready_for_flow: false",
        '"provider_output_count": 0',
        "provider output count: `0`",
        '"provider_insertion_count": 0',
        "provider-backed insertions: 0",
        '"ready_task_count": 0',
    )
    if any(token in text for token in provider_blocked_tokens):
        failures.append("local_provider_output_not_materialized")
    return failures


def _expects_provider_video_materialization(work_item: WorkItem) -> bool:
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.type,
            work_item.phase or "",
            work_item.expected_output or "",
        ]
    ).lower()
    if any(
        token in text
        for token in (
            "source-parity",
            "source parity",
            "parity-and-port",
            "bridge parity",
            "regression-test",
            "regression tests",
        )
    ):
        return False
    return any(
        token in text
        for token in (
            "real-seedance-single-probe",
            "real seedance single probe",
            "real-provider",
            "real provider",
            "provider-backed",
            "provider backed",
            "provider mp4",
            "provider output",
            "seedance probe",
            "ai video",
            "ai-video",
            "final-evidence-package",
            "final evidence package",
            "stage1-seedance-gate-review",
        )
    )


def _browser_gate_has_local_fallback_evidence(text: str) -> bool:
    return (
        "ffprobe" in text
        and any(phrase in text for phrase in ("fallback evidence", "evidence used instead"))
        and any(
            phrase in text
            for phrase in (
                "local player",
                "local-player",
                "player html",
                "player metadata",
                "metadata",
            )
        )
    )


def _enforces_local_delivery_blocking_evidence(work_item: WorkItem) -> bool:
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.type,
            work_item.expected_output or "",
        ]
    ).lower()
    if any(
        token in text
        for token in (
            "material-screening",
            "screening-and-selection",
            "candidate-screening",
            "素材筛选",
        )
    ):
        return False
    return any(
        token in text
        for token in (
            "phase1-demo",
            "demo-comparison",
            "acceptance-review",
            "assembly-and-creator",
            "creator-integration",
            "final-delivery",
            "stage1",
            "transition-generation",
            "mixed-edit-repair",
            "review-ready",
            "deliverable",
            "player-experience",
            "验收",
            "交付",
        )
    )


def _runtime_text_blocking_failures(output: KunTaskExecutionOutput) -> list[str]:
    text = " ".join(
        [
            output.answer,
            json.dumps(output.raw, ensure_ascii=False, sort_keys=True)
            if isinstance(output.raw, dict)
            else str(output.raw),
        ]
    ).lower()
    failures: list[str] = []
    if (
        "reject_weak_ad_grade_output" in text
        or '"human_simulation_accepted": false' in text
        or "decision: rejected" in text
    ):
        failures.append("runtime_human_simulation_rejected")
    if (
        "blocked_not_deliverable_demo" in text
        or "no client-deliverable mp4" in text
        or "rendered_review_ready_demo_missing" in text
    ):
        failures.append("runtime_deliverable_demo_blocked")
    return failures


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


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
        f"Control Plane execution attempt timestamp: {datetime.now(UTC).isoformat()}",
        (
            "Return the result as text. The Control Plane runner will persist artifacts, "
            "reports, and gate evidence; do not block a review/planning work item only "
            "because you cannot write the final report file yourself."
        ),
        (
            "Create a new unique output directory for this attempt. Do not delete, overwrite, "
            "or clean existing outputs, and do not run destructive cleanup commands such as "
            "rm -rf, git clean, git reset --hard, or git checkout --."
        ),
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


def _artifact_kind_for_work_item(work_item: WorkItem) -> str:
    text = f"{work_item.type}\n{work_item.phase or ''}\n{work_item.expected_output}".lower()
    if work_item.type in {"test", "retest"}:
        return "test_result"
    if work_item.type == "review":
        return "review"
    if _expects_report(text):
        return "report"
    return "answer"


def _artifact_supports_for_work_item(
    *,
    work_item: WorkItem,
    artifact_kind: str,
    capability_supports: list[str],
) -> list[str]:
    text = f"{work_item.type}\n{work_item.phase or ''}\n{work_item.expected_output}".lower()
    supports = [
        "kun_runtime_task_output",
        "real_task_execution",
        *capability_supports,
        *work_item.skill_refs,
    ]
    if artifact_kind == "report" or _expects_report(text):
        supports.extend(["runtime_report", "report"])
    if artifact_kind == "review":
        supports.extend(["runtime_review", "review", "runtime_report", "report"])
    if artifact_kind == "test_result":
        supports.extend(["runtime_test_evidence", "test_result"])
    if work_item.rollback_refs or _expects_rollback(text):
        supports.extend(["runtime_rollback_reference", "rollback_plan", "rollback_refs"])
    return _dedupe(supports)


def _expects_report(text: str) -> bool:
    return any(token in text for token in ("report", "summary", "audit", "review", "验收", "报告"))


def _expects_rollback(text: str) -> bool:
    return any(token in text for token in ("rollback", "checkpoint", "回滚"))


def _executor_exception_failure_category(summary: str) -> str:
    text = summary.lower()
    if any(
        token in text
        for token in (
            "connect call failed",
            "connection refused",
            "network unreachable",
            "temporary failure in name resolution",
            "database is unavailable",
            "could not connect to server",
            "connection reset",
            "timeout",
            "timed out",
        )
    ):
        return "environment_failure"
    if any(token in text for token in ("permission denied", "operation not permitted", "errno 1")):
        return "permission_failure"
    return "tool_failure"


def _work_item_gate(
    *,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    local_evidence_artifacts: list[ArtifactRecord],
    output: KunTaskExecutionOutput,
    task_type: str,
) -> GateEvaluation:
    failures = _runtime_work_item_failures(
        work_item=work_item,
        artifact=artifact,
        output=output,
        local_evidence_artifacts=local_evidence_artifacts,
    )
    passed = not failures
    artifact_refs = [artifact.artifact_id, *[item.artifact_id for item in local_evidence_artifacts]]
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
            "local_blocking_evidence_absent": 1.0
            if not any(failure.startswith("local_") for failure in failures)
            else 0.0,
        },
        thresholds={"result_quality": 0.8},
        hard_gate_failures=failures,
        evidence_refs=artifact_refs,
        artifact_refs=artifact_refs,
        source_freshness="fresh",
        failure_category=None if passed else _runtime_failure_category(failures),
        root_cause="" if passed else _runtime_failure_root_cause(failures),
        responsibility_scope="kun_auto",
        confidence=0.76 if passed else 0.55,
        next_action="continue" if passed else "needs_repair",
        next_state="running" if passed else "repairing",
        governance_signal="kun_runtime_task_executed",
        created_by=KunRuntimeTaskRunner.runner_identity,
    )


def _runtime_failure_category(failures: list[str]) -> str:
    if "local_provider_network_blocked" in failures:
        return "environment_failure"
    if failures and set(failures) <= {"local_browser_player_gate_blocked"}:
        return "environment_failure"
    return "delivery_failure"


def _runtime_work_item_failures(
    *,
    work_item: WorkItem,
    artifact: ArtifactRecord,
    output: KunTaskExecutionOutput,
    local_evidence_artifacts: list[ArtifactRecord],
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
        answer="\n".join(
            [
                output.answer,
                _local_evidence_contract_text(local_evidence_artifacts),
            ]
        ),
    ):
        failures.append("expected_output_not_addressed")
    if work_item.type in {"test", "retest"} and not _looks_like_test_evidence(output):
        failures.append("test_evidence_missing")
    if _requires_local_runtime_evidence(work_item) and not local_evidence_artifacts:
        failures.append("local_runtime_evidence_missing")
    failures.extend(_runtime_text_blocking_failures(output))
    failures.extend(
        _local_evidence_blocking_failures(
            local_evidence_artifacts,
            work_item=work_item,
        )
    )
    return _dedupe(failures)


def _runtime_failure_root_cause(failures: list[str]) -> str:
    if "local_runtime_evidence_missing" in failures:
        return (
            "Runner returned text but did not produce local RainFlow evidence files under the "
            "isolated workspace output tree, so the work cannot be treated as completed."
        )
    if any(failure.startswith("local_") for failure in failures):
        if "local_provider_network_blocked" in failures:
            return (
                "Local RainFlow evidence shows the required provider-backed video did not "
                "materialize because the external provider endpoint could not be reached."
            )
        if "local_provider_output_not_materialized" in failures:
            return (
                "Local RainFlow evidence shows the work still has no insertable "
                "provider-backed video output, so it cannot be treated as complete."
            )
        return (
            "Local RainFlow evidence contains blocking review, demo, comparison, or AI-stage "
            "gate failures that must be fixed before this work can pass."
        )
    if "capability_not_consumed_by_runner" in failures:
        return "Required capability refs were attached, but the runner did not emit an executable capability receipt."
    if "test_evidence_missing" in failures:
        return "The work item expected test or retest evidence, but the runner output did not include a credible test result."
    if "expected_output_not_addressed" in failures:
        return "The runner output did not address the work item's expected output contract."
    if "runtime_task_output_missing" in failures:
        return "The runner returned an empty answer."
    if "runtime_task_status_not_done" in failures:
        return "The runner did not report a done or partial status."
    return "Runtime work item gate failed and requires repair before the mission can proceed."


def _requires_local_runtime_evidence(work_item: WorkItem) -> bool:
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.type,
            work_item.expected_output or "",
        ]
    ).lower()
    if "rainflow" not in text and "phase1" not in text and "adflow" not in text:
        return False
    if work_item.type in {"test", "retest"}:
        return True
    return any(
        token in text
        for token in (
            "mixed-edit-repair",
            "material-screening",
            "screening-and-selection",
            "assembly-and-creator",
            "creator-integration",
            "demo-comparison",
            "acceptance-review",
            "stage1",
            "transition-generation",
            "final-delivery",
            "demo package",
            "review-ready",
            "素材筛选",
            "验收",
            "交付",
        )
    )


def _expected_output_addressed(*, expected_output: str, answer: str) -> bool:
    expected = _keywords(expected_output)
    if not expected:
        return bool(answer.strip())
    answered = _keywords(answer)
    overlap = len(expected.intersection(answered))
    return overlap >= min(2, len(expected)) or len(answer.strip()) >= 240


def _local_evidence_contract_text(artifacts: list[ArtifactRecord]) -> str:
    snippets: list[str] = []
    for artifact in artifacts[:12]:
        path = Path(artifact.path_or_uri)
        try:
            if not path.is_file() or path.stat().st_size > 128_000:
                continue
            snippets.append(path.read_text(encoding="utf-8", errors="ignore")[:8_000])
        except OSError:
            continue
    return "\n".join(snippets)


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
        _keyword_stem(token)
        for token in re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]{2,}", text.lower())
        if token not in {"the", "and", "with", "this", "that", "任务", "输出", "完成"}
    }


def _keyword_stem(token: str) -> str:
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


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
    if _is_game_production_mission(control_plane, work_item.mission_id):
        followups = _game_production_strategy_followups(
            control_plane=control_plane,
            work_item=work_item,
            strategy_artifact_ref=artifact.artifact_id,
        )
    else:
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


def _is_game_production_mission(control_plane: InMemoryControlPlane, mission_id: str) -> bool:
    mission = control_plane.missions.get(mission_id)
    if mission is None or mission.task_type != "product_development":
        return False
    contract = control_plane.contracts.get(mission.execution_contract_ref or "")
    if contract is None:
        return False
    delivery_contract = contract.delivery_contract
    production_mode = str(delivery_contract.get("production_mode") or "").strip()
    if production_mode:
        from kun.control_plane.game_production import SUPPORTED_GAME_PRODUCTION_MODES

        return production_mode in SUPPORTED_GAME_PRODUCTION_MODES
    return any(
        item.mission_id == mission_id
        and item.owner == "kun-game-production-runner"
        and (
            "game" in item.work_item_id.lower()
            or any(
                token in (item.phase or "").lower()
                for token in (
                    "visual-product",
                    "sandbox-dynamics",
                    "image-object",
                    "commercial-game",
                )
            )
        )
        for item in control_plane.work_items.values()
    )


def _game_production_strategy_followups(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    strategy_artifact_ref: str,
) -> list[WorkItem]:
    from kun.control_plane.game_production import (
        EXTERNAL_SUPERVISOR_GATE_OWNER,
        KUN_GAME_PRODUCTION_RUNNER_OWNER,
    )

    base_slug = _slug(work_item.work_item_id)
    recovery_refs = [strategy_artifact_ref, *work_item.recovery_refs]
    workspace_ref = work_item.workspace_ref
    sandbox_ref = work_item.sandbox_ref
    resource_locks = list(work_item.resource_locks)
    mission = control_plane.missions.get(work_item.mission_id)
    contract = (
        control_plane.contracts.get(mission.execution_contract_ref or "") if mission else None
    )
    delivery_contract = contract.delivery_contract if contract is not None else {}
    project_path = str(delivery_contract.get("project_path") or "").strip()
    if project_path and not workspace_ref:
        workspace_ref = f"workspace://{project_path}"
    if project_path and not resource_locks:
        resource_locks = [f"workspace:{Path(project_path).expanduser().resolve()}"]
    if not sandbox_ref:
        sandbox_ref = f"sandbox://{work_item.mission_id}/{work_item.work_item_id}/strategy-rework"
    common = {
        "mission_id": work_item.mission_id,
        "task_plan_version": work_item.task_plan_version,
        "priority": max(0, work_item.priority - 1),
        "workspace_ref": workspace_ref,
        "sandbox_ref": sandbox_ref,
        "resource_locks": resource_locks,
        "required_capability_refs": list(work_item.required_capability_refs),
        "external_source_refs": list(work_item.external_source_refs),
        "recovery_refs": recovery_refs,
    }
    steps = [
        (
            "visual-product-iteration",
            "execution",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Rework the game toward a commercial visual standard: original character art, "
            "image-based generated objects, readable tablet layout, and non-label object "
            "presentation. Do not close on mechanism-only proof.",
        ),
        (
            "image-object-interaction-iteration",
            "execution",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Make word-created images behave like game objects: drag existing entities instead "
            "of duplicating them, show contact feedback, and make food/tool/vehicle/character "
            "interactions visible on stage.",
        ),
        (
            "sandbox-dynamics-iteration",
            "execution",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Deepen physical and causal sandbox play with object combinations, attachments, "
            "NPC reactions, failure feedback, undo, and repeatable multi-solution goals.",
        ),
        (
            "commercial-game-polish-iteration",
            "execution",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Polish the first-screen feel, animation, sound/feedback hooks, spacing, "
            "touch targets, and emotional reward loop until it reads like a playable game.",
        ),
        (
            "internal-test",
            "test",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Run build, unit checks, long simulation, fun test, browser/static playtest, "
            "and product interaction checks for the stricter game loop.",
        ),
        (
            "supervisor-gate",
            "review",
            EXTERNAL_SUPERVISOR_GATE_OWNER,
            "Review the game against final player experience standards, emphasizing real "
            "player feel over self-scored gate pass/fail.",
        ),
        (
            "benchmark-residual-audit",
            "review",
            EXTERNAL_SUPERVISOR_GATE_OWNER,
            "Audit residual gaps against the benchmark experience across UI, character, "
            "image object quality, drag feel, causal reactions, and open-ended puzzle depth.",
        ),
        (
            "final-delivery",
            "merge",
            KUN_GAME_PRODUCTION_RUNNER_OWNER,
            "Only prepare delivery if the stricter product, browser, residual, and player "
            "experience gates produce real evidence. Otherwise keep the mission running.",
        ),
    ]
    followups: list[WorkItem] = []
    previous_id = work_item.work_item_id
    for index, (phase, item_type, owner, expected_output) in enumerate(steps, start=1):
        item_id = f"work-game-rework-{base_slug}-{index:02d}-{phase}"
        item_locks = list(common["resource_locks"])
        if phase == "final-delivery":
            item_locks = [*item_locks, f"mission:{work_item.mission_id}"]
        followups.append(
            WorkItem(
                work_item_id=item_id,
                type=item_type,  # type: ignore[arg-type]
                owner=owner,
                dependencies=[previous_id],
                phase=phase,
                expected_output=expected_output,
                idempotency_key=(
                    f"game-strategy-final-delivery:{work_item.work_item_id}"
                    if phase == "final-delivery"
                    else None
                ),
                **{**common, "resource_locks": item_locks},
            )
        )
        previous_id = item_id
    return followups


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
    related_artifacts = _dedupe_dependency_retry_artifacts(related_artifacts)
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


def _dedupe_dependency_retry_artifacts(
    artifacts: list[ArtifactRecord],
) -> list[ArtifactRecord]:
    """Keep the latest logical artifact for the same dependency output.

    Retry runs can emit multiple report/diff/answer artifacts for the same
    dependency work item and logical output path. Merge should compare the
    latest retry result, not treat superseded retries as cross-worker conflicts.
    """

    latest_by_output: dict[tuple[str | None, str, str], ArtifactRecord] = {}
    passthrough: list[ArtifactRecord] = []
    for artifact in artifacts:
        if artifact.kind not in {"diff", "report", "answer"}:
            passthrough.append(artifact)
            continue
        key = (artifact.work_item_id, artifact.kind, artifact.path_or_uri)
        latest_by_output[key] = artifact
    return [*passthrough, *latest_by_output.values()]


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _slug(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-")
    if not safe:
        return "item"
    if len(safe) <= 80:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:67].rstrip('-_')}-{digest}"


__all__ = ["KunRuntimeTaskRunner", "KunTaskExecutionOutput"]
