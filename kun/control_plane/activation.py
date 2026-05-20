"""Runtime feature activation for every KUN V6 work item.

This layer keeps productized capabilities from becoming passive shelfware:
before a runner executes, the daemon records which production capabilities,
skills, workspace boundary, checkpoint, and rollback handles apply to that
specific work item.  Runners can still add richer behavior, but the control
plane now has a default activation path for all long-running tasks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.v6 import ArtifactRecord, ExecutionContract, TaskPlan, WorkItem
from kun.control_plane.workspace_snapshot import create_workspace_snapshot

if TYPE_CHECKING:
    from kun.control_plane.runtime import InMemoryControlPlane


class WorkItemActivation(BaseModel):
    """Auditable activation bundle applied before a work item runs."""

    model_config = ConfigDict(extra="forbid")

    work_item: WorkItem
    artifacts: list[ArtifactRecord] = Field(default_factory=list)


def activate_work_item_features(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    capability_policy: CapabilityExecutionPolicy,
    actor: str,
    observed_at: datetime,
) -> WorkItemActivation:
    """Attach default runtime capabilities and execution safeguards to a work item."""

    mission = control_plane.missions[work_item.mission_id]
    task_plan = control_plane.task_plans.get(mission.current_plan_version or "")
    contract = control_plane.contracts.get(mission.execution_contract_ref or "")
    capability_refs = _merge_unique(
        work_item.required_capability_refs,
        capability_policy.capability_profile_refs,
    )
    skill_refs = _merge_unique(work_item.skill_refs, _match_skill_refs(mission.objective, task_plan, work_item))
    external_refs = _merge_unique(
        work_item.external_source_refs,
        _external_source_refs(task_plan=task_plan, work_item=work_item),
    )

    workspace_path = _workspace_path(contract)
    checkpoint_artifact = _build_checkpoint_artifact(
        control_plane=control_plane,
        work_item=work_item,
        workspace_path=workspace_path,
        actor=actor,
        observed_at=observed_at,
    )
    workspace_ref = work_item.workspace_ref
    sandbox_ref = work_item.sandbox_ref
    checkpoint_refs = list(work_item.checkpoint_refs)
    rollback_refs = list(work_item.rollback_refs)
    artifacts: list[ArtifactRecord] = []
    if checkpoint_artifact is not None:
        artifacts.append(checkpoint_artifact)
        checkpoint_refs = _merge_unique(checkpoint_refs, [checkpoint_artifact.artifact_id])
        rollback_refs = _merge_unique(rollback_refs, [checkpoint_artifact.artifact_id])
        workspace_ref = workspace_ref or f"workspace://{workspace_path}"
        sandbox_ref = sandbox_ref or f"sandbox://{work_item.mission_id}/{work_item.work_item_id}"

    activation_artifact = _activation_artifact(
        work_item=work_item,
        actor=actor,
        observed_at=observed_at,
        payload={
            "work_item_id": work_item.work_item_id,
            "mission_id": work_item.mission_id,
            "capability_policy_id": capability_policy.policy_id,
            "capability_profile_refs": capability_refs,
            "capability_directive_count": len(capability_policy.directives),
            "skill_refs": skill_refs,
            "external_source_refs": external_refs,
            "workspace_ref": workspace_ref,
            "sandbox_ref": sandbox_ref,
            "checkpoint_refs": checkpoint_refs,
            "rollback_refs": rollback_refs,
            "activated_features": [
                "production_capability_policy",
                "skill_trigger_scan",
                "external_information_signal",
                "workspace_sandbox_boundary",
                "checkpoint_and_rollback_reference",
                "qi_nuo_feedback_route",
            ],
        },
        supports=[
            "runtime_feature_activation",
            "production_capability_policy",
            "skill_trigger_scan",
            "external_information_signal",
            "workspace_sandbox_boundary",
            "checkpoint_and_rollback_reference",
            "qi_nuo_feedback_route",
            capability_policy.policy_id,
            *capability_refs,
            *skill_refs,
        ],
    )
    artifacts.append(activation_artifact)

    updated = work_item.model_copy(
        update={
            "required_capability_refs": capability_refs,
            "skill_refs": skill_refs,
            "external_source_refs": external_refs,
            "workspace_ref": workspace_ref,
            "sandbox_ref": sandbox_ref,
            "checkpoint_refs": checkpoint_refs,
            "rollback_refs": rollback_refs,
        }
    )
    return WorkItemActivation(work_item=updated, artifacts=artifacts)


def _match_skill_refs(
    objective: str,
    task_plan: TaskPlan | None,
    work_item: WorkItem,
) -> list[str]:
    prompt_parts = [
        objective,
        work_item.type,
        work_item.expected_output,
    ]
    if task_plan is not None:
        prompt_parts.extend(
            [
                " ".join(task_plan.evidence_plan),
                " ".join(task_plan.decomposition),
                " ".join(task_plan.test_plan),
                " ".join(task_plan.constraints),
            ]
        )
    prompt = "\n".join(str(part) for part in prompt_parts if part)
    try:
        from kun.skills.loader import get_registry

        registry = get_registry()
        refs = [skill_id for skill_id, _, _ in registry.match_auto_triggers(prompt)]
        available = set(registry.names())
        text = prompt.lower()
        if work_item.type == "research" and "research-web-fetch" in available:
            refs.append("research-web-fetch")
        if work_item.type in {"execution", "test", "repair", "retest"}:
            if "coding-pytest" in available and any(
                token in text for token in ("code", "test", "pytest", "app", "mvp", "开发")
            ):
                refs.append("coding-pytest")
            if "os-shell" in available:
                refs.append("os-shell")
        if any(token in text for token in (".csv", "spreadsheet", "表格")) and (
            "data-csv-query" in available
        ):
            refs.append("data-csv-query")
        if any(token in text for token in ("markdown", "doc", "report", "方案", "文档")) and (
            "writing-markdown" in available
        ):
            refs.append("writing-markdown")
        return _merge_unique(refs)
    except Exception:
        return []


def _external_source_refs(*, task_plan: TaskPlan | None, work_item: WorkItem) -> list[str]:
    refs: list[str] = []
    if task_plan is not None and task_plan.evidence_plan:
        refs.append(f"evidence-plan://{task_plan.plan_id}")
    text = f"{work_item.type}\n{work_item.expected_output}".lower()
    if work_item.type == "research" or any(
        token in text
        for token in (
            "research",
            "source",
            "external",
            "benchmark",
            "reference",
            "调研",
            "资料",
            "搜索",
            "引用",
        )
    ):
        refs.append(f"external-info-needed://{work_item.work_item_id}")
    return refs


def _workspace_path(contract: ExecutionContract | None) -> str | None:
    if contract is None:
        return None
    candidates: list[Any] = [
        contract.delivery_contract,
        contract.risk_policy,
        contract.rollback_policy,
    ]
    keys = (
        "workspace_path",
        "project_path",
        "repo_path",
        "target_path",
        "output_dir",
        "delivery_path",
    )
    for mapping in candidates:
        found = _find_first_path(mapping, keys)
        if found:
            return found
    return None


def _find_first_path(value: Any, keys: Iterable[str]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            found = _find_first_path(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_first_path(item, keys)
            if found:
                return found
    return None


def _build_checkpoint_artifact(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    workspace_path: str | None,
    actor: str,
    observed_at: datetime,
) -> ArtifactRecord | None:
    if not workspace_path:
        return None
    snapshot = create_workspace_snapshot(
        control_plane=control_plane,
        work_item=work_item,
        workspace_path=workspace_path,
        actor=actor,
        observed_at=observed_at,
    )
    if snapshot is None:
        return None
    return snapshot.artifact


def _activation_artifact(
    *,
    work_item: WorkItem,
    actor: str,
    observed_at: datetime,
    payload: dict[str, Any],
    supports: list[str],
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact-runtime-activation-{_slug(work_item.work_item_id)}-{_compact_time(observed_at)}",
        kind="report",
        path_or_uri=f"control-plane://activation/{work_item.mission_id}/{work_item.work_item_id}/{observed_at.isoformat()}",
        content_hash=_hash_payload(payload),
        created_by=actor,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=_merge_unique(supports, ["runtime_feature_activation"]),
        freshness="fresh",
        source_quality="primary",
    )


def _merge_unique(*groups: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _compact_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"
