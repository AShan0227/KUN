"""Default V6 work-item preflight execution.

Activation records which capabilities and skills should apply.  Preflight is
the matching execution step: it runs safe, bounded builtin skills before the
main runner so external information, local workspace inspection, and test
signals become real artifacts instead of passive references.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import ArtifactKind, ArtifactRecord, TaskPlan, WorkItem
from kun.skills.dispatcher import SkillResult

if TYPE_CHECKING:
    from kun.control_plane.runtime import InMemoryControlPlane

_SKILL_ALIASES: dict[str, str] = {
    "research-web-fetch": "web-search",
    "coding-pytest": "shell-exec",
    "os-shell": "shell-exec",
    "data-csv-query": "csv-query",
}


class WorkItemPreflight(BaseModel):
    """Auditable preflight result for one work item."""

    model_config = ConfigDict(extra="forbid")

    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    failed_skill_ids: list[str] = Field(default_factory=list)


def run_work_item_preflight(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    actor: str,
    observed_at: datetime,
) -> WorkItemPreflight:
    """Run bounded default skills for one V6 work item and persist artifacts."""

    skill_runs = _planned_skill_runs(control_plane=control_plane, work_item=work_item)
    artifacts: list[ArtifactRecord] = []
    failed: list[str] = []
    for skill_id, params in skill_runs:
        result = _run_skill(skill_id, params)
        if not result.ok:
            failed.append(skill_id)
        artifact = _persist_skill_result(
            control_plane=control_plane,
            work_item=work_item,
            actor=actor,
            observed_at=observed_at,
            skill_id=skill_id,
            params=params,
            result=result,
        )
        artifacts.append(artifact)
    return WorkItemPreflight(artifacts=artifacts, failed_skill_ids=failed)


def _planned_skill_runs(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> list[tuple[str, dict[str, Any]]]:
    _autoload_skills()
    mission = control_plane.missions[work_item.mission_id]
    plan = control_plane.task_plans.get(mission.current_plan_version or "")
    contract = control_plane.contracts.get(mission.execution_contract_ref or "")
    workspace = _workspace_path(work_item.workspace_ref) or _workspace_path_from_contract(contract)
    text = _work_item_text(mission.objective, plan, work_item)
    runs: list[tuple[str, dict[str, Any]]] = []
    executable_refs = _executable_skill_refs(work_item.skill_refs)

    if _needs_external_info(work_item, text) and _network_preflight_enabled():
        runs.append(
            (
                "web-search",
                {
                    "query": _search_query(mission.objective, plan, work_item),
                    "max_results": 3,
                },
            )
        )

    csv_path = _first_path_with_suffix(text, ".csv")
    if csv_path and "csv-query" in executable_refs:
        runs.append(("csv-query", {"path": csv_path, "sql": "SELECT * FROM data LIMIT 10"}))

    pdf_path = _first_path_with_suffix(text, ".pdf")
    if pdf_path and "pdf-read" in executable_refs:
        runs.append(("pdf-read", {"path": pdf_path, "max_chars": 8000}))

    if "shell-exec" in executable_refs and _should_inspect_workspace(work_item, text):
        runs.append(
            (
                "shell-exec",
                {
                    "command": "find . -maxdepth 2 -type f",
                    "cwd": workspace or os.getcwd(),
                    "timeout_sec": 10,
                },
            )
        )

    if workspace and _should_run_pytest(work_item, text):
        runs.append(
            (
                "shell-exec",
                {
                    "command": ".venv/bin/python -m pytest -q",
                    "cwd": workspace,
                    "timeout_sec": 120,
                },
            )
        )

    return _dedupe_runs(runs)


def _run_skill(skill_id: str, params: dict[str, Any]) -> SkillResult:
    from kun.skills.dispatcher import dispatch

    return _run_async(dispatch(skill_id, params))


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    def _runner() -> Any:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def _persist_skill_result(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    actor: str,
    observed_at: datetime,
    skill_id: str,
    params: dict[str, Any],
    result: SkillResult,
) -> ArtifactRecord:
    payload = {
        "schema": "kun-v6-preflight-skill-result-v1",
        "mission_id": work_item.mission_id,
        "work_item_id": work_item.work_item_id,
        "skill_id": skill_id,
        "params": _redact_params(params),
        "result": result.model_dump(mode="json"),
        "observed_at": observed_at.isoformat(),
    }
    path = _preflight_path(control_plane, work_item, skill_id, observed_at)
    _write_json_atomic(path, payload)
    supports = [
        "skill_preflight",
        "runtime_feature_activation",
        f"skill:{skill_id}",
        "qi_nuo_feedback_route",
    ]
    if result.ok:
        supports.append("skill_preflight_pass")
    else:
        supports.append("skill_preflight_failure")
    return ArtifactRecord(
        artifact_id=f"artifact-preflight-{_slug(work_item.work_item_id)}-{_slug(skill_id)}-{_compact_time(observed_at)}",
        kind=_artifact_kind_for_skill(skill_id),
        path_or_uri=str(path),
        content_hash=_hash_payload(payload),
        created_by=actor,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=supports,
        freshness="fresh",
        source_quality="primary" if result.ok else "unknown",
    )


def _autoload_skills() -> None:
    try:
        from kun.skills.dispatcher import autoload_builtins

        autoload_builtins()
    except Exception:
        return


def _executable_skill_refs(skill_refs: list[str]) -> set[str]:
    refs = set(skill_refs)
    for ref in skill_refs:
        alias = _SKILL_ALIASES.get(ref)
        if alias:
            refs.add(alias)
    return refs


def _work_item_text(objective: str, plan: TaskPlan | None, work_item: WorkItem) -> str:
    parts = [
        objective,
        work_item.type,
        work_item.expected_output,
        " ".join(work_item.external_source_refs),
        " ".join(work_item.skill_refs),
    ]
    if plan is not None:
        parts.extend(
            [
                " ".join(plan.evidence_plan),
                " ".join(plan.decomposition),
                " ".join(plan.test_plan),
                " ".join(plan.constraints),
            ]
        )
    return "\n".join(part for part in parts if part)


def _needs_external_info(work_item: WorkItem, text: str) -> bool:
    lowered = text.lower()
    return bool(work_item.external_source_refs) or work_item.type == "research" or any(
        token in lowered
        for token in (
            "latest",
            "recent",
            "source",
            "research",
            "benchmark",
            "external",
            "调研",
            "资料",
            "搜索",
            "引用",
            "最新",
        )
    )


def _network_preflight_enabled() -> bool:
    return os.getenv("KUN_CONTROL_PLANE_NETWORK_PREFLIGHT", "0") == "1"


def _should_inspect_workspace(work_item: WorkItem, text: str) -> bool:
    lowered = text.lower()
    return work_item.workspace_ref is not None and any(
        token in lowered
        for token in ("code", "test", "file", "artifact", "app", "mvp", "开发", "测试", "产物")
    )


def _should_run_pytest(work_item: WorkItem, text: str) -> bool:
    lowered = text.lower()
    return work_item.type in {"test", "retest"} or "pytest" in lowered


def _search_query(objective: str, plan: TaskPlan | None, work_item: WorkItem) -> str:
    query = " ".join(
        part
        for part in [
            objective,
            work_item.expected_output,
            " ".join(plan.evidence_plan[:2]) if plan is not None else "",
        ]
        if part
    )
    return query[:300]


def _first_path_with_suffix(text: str, suffix: str) -> str | None:
    pattern = r"(?P<path>\S+" + re.escape(suffix) + r")\b"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group("path") if match else None


def _workspace_path(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("workspace://"):
        return value.removeprefix("workspace://")
    return value


def _workspace_path_from_contract(contract: Any) -> str | None:
    if contract is None:
        return None
    for mapping in (
        getattr(contract, "delivery_contract", {}),
        getattr(contract, "risk_policy", {}),
        getattr(contract, "rollback_policy", {}),
    ):
        found = _find_first_path(mapping)
        if found:
            return found
    return None


def _find_first_path(value: Any) -> str | None:
    keys = ("workspace_path", "project_path", "repo_path", "target_path", "output_dir")
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


def _artifact_kind_for_skill(skill_id: str) -> ArtifactKind:
    if skill_id == "web-search":
        return "source"
    if skill_id in {"shell-exec", "python-exec"}:
        return "test_result"
    if skill_id in {"csv-query", "pdf-read"}:
        return "evidence"
    return "log"


def _preflight_path(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    skill_id: str,
    observed_at: datetime,
) -> Path:
    store = getattr(control_plane, "store", None)
    store_path = getattr(store, "_path", None)
    if store_path is not None:
        base = Path(store_path).parent / "preflight"
    else:
        base = Path(".kun-local") / "preflight"
    return (
        base
        / work_item.mission_id
        / work_item.work_item_id
        / f"{_slug(skill_id)}-{_compact_time(observed_at)}.json"
    )


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(params)
    for key in list(redacted):
        if any(token in key.lower() for token in ("token", "secret", "password", "key")):
            redacted[key] = "[redacted]"
    return redacted


def _dedupe_runs(runs: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for skill_id, params in runs:
        key = json.dumps([skill_id, params], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append((skill_id, params))
    return out


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        tmp = Path(handle.name)
    os.replace(tmp, path)


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _compact_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"
