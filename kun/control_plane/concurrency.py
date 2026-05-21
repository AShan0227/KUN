"""Concurrency, lock, sandbox, and merge-governance helpers for KUN V6.

This module is the product boundary between "one daemon doing a batch" and a
real Control Plane that can coordinate multiple worker slots and multiple
daemon processes.  The first production implementation remains local-first, but
the contracts are intentionally durable: leases can live in a file/DB-backed
store, workers report slots, resource conflicts are auditable, sandbox mode is
explicit, and merge work can fail on concrete conflicts instead of blindly
attaching artifacts.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import ArtifactRecord, WorkItem

try:  # pragma: no cover - fcntl is available on macOS/Linux CI, fallback is tested by behavior.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


WorkerSlotStatus = Literal["idle", "running", "waiting_lock", "blocked", "unavailable"]
SandboxIsolationMode = Literal["workspace_snapshot", "container_required", "external_container"]
MergeConflictSeverity = Literal["info", "warning", "blocking"]


class WorkerPoolConfig(BaseModel):
    """Operator-visible worker-pool configuration."""

    model_config = ConfigDict(extra="forbid")

    pool_id: str = "kun-local-worker-pool"
    machine_id: str = "local"
    worker_count: int = Field(default=1, ge=1, le=256)
    worker_id_prefix: str = "worker"


class WorkerSlotSnapshot(BaseModel):
    """One worker slot state written into daemon progress and cockpit views."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str
    worker_id: str
    machine_id: str
    status: WorkerSlotStatus
    mission_id: str | None = None
    work_item_id: str | None = None
    runner_identity: str | None = None
    resource_locks: list[str] = Field(default_factory=list)
    sandbox_ref: str | None = None
    waiting_reason: str = ""


class ResourceLockLease(BaseModel):
    """A durable lease for a resource such as a workspace or mission merge lane."""

    model_config = ConfigDict(extra="forbid")

    lease_id: str
    resource_ref: str
    holder_id: str
    daemon_id: str
    worker_id: str
    mission_id: str
    work_item_id: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime

    def active(self, *, now: datetime) -> bool:
        return self.expires_at > now


class ResourceLockConflict(BaseModel):
    """Why a work item could not run yet due to an already-held resource."""

    model_config = ConfigDict(extra="forbid")

    resource_ref: str
    waiting_work_item_id: str
    holder_id: str
    holder_work_item_id: str | None = None
    holder_daemon_id: str | None = None
    expires_at: datetime | None = None
    waiting_reason: str


class ResourceLockAcquisition(BaseModel):
    """Result of trying to atomically acquire all resource locks for one item."""

    model_config = ConfigDict(extra="forbid")

    acquired: bool
    holder_id: str
    leases: list[ResourceLockLease] = Field(default_factory=list)
    conflicts: list[ResourceLockConflict] = Field(default_factory=list)


class SandboxIsolationSpec(BaseModel):
    """The sandbox mode actually requested for a work item."""

    model_config = ConfigDict(extra="forbid")

    sandbox_ref: str
    mission_id: str
    work_item_id: str
    mode: SandboxIsolationMode
    workspace_ref: str | None = None
    root_refs: list[str] = Field(default_factory=list)
    writable_refs: list[str] = Field(default_factory=list)
    network_policy: Literal["inherit", "disabled", "allowlist"] = "inherit"
    container_runtime: str | None = None
    checkpoint_refs: list[str] = Field(default_factory=list)
    rollback_refs: list[str] = Field(default_factory=list)
    text: str


class MergeConflictIssue(BaseModel):
    """One concrete merge risk or conflict."""

    model_config = ConfigDict(extra="forbid")

    severity: MergeConflictSeverity
    code: str
    summary: str
    refs: list[str] = Field(default_factory=list)


class MergeGovernanceReport(BaseModel):
    """Conflict-aware report for KUN merge work items."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "kun-v6-merge-governance-v1"
    mission_id: str
    work_item_id: str
    dependency_refs: list[str] = Field(default_factory=list)
    merged_artifact_refs: list[str] = Field(default_factory=list)
    issues: list[MergeConflictIssue] = Field(default_factory=list)
    pass_merge_gate: bool

    @property
    def blocking_issue_codes(self) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == "blocking"]


class FileResourceLockStore:
    """Small durable resource-lock store suitable for local multi-daemon dogfood.

    It uses an advisory OS file lock and atomic JSON replacement.  The API is
    intentionally DB-friendly: acquire all-or-nothing, release by holder, and
    list active leases.  A future Redis/Postgres adapter can preserve the same
    semantics without changing daemon scheduling.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def acquire_many(
        self,
        *,
        resources: list[str],
        holder_id: str,
        daemon_id: str,
        worker_id: str,
        work_item: WorkItem,
        now: datetime,
        ttl: timedelta,
    ) -> ResourceLockAcquisition:
        unique_resources = _dedupe(resources)
        if not unique_resources:
            return ResourceLockAcquisition(acquired=True, holder_id=holder_id)
        with self._locked():
            leases = self._load_unlocked(now=now)
            conflicts: list[ResourceLockConflict] = []
            for resource_ref in unique_resources:
                held = leases.get(resource_ref)
                if held is None or held.holder_id == holder_id:
                    continue
                conflicts.append(
                    ResourceLockConflict(
                        resource_ref=resource_ref,
                        waiting_work_item_id=work_item.work_item_id,
                        holder_id=held.holder_id,
                        holder_work_item_id=held.work_item_id,
                        holder_daemon_id=held.daemon_id,
                        expires_at=held.expires_at,
                        waiting_reason=(
                            "另一个 worker 正在使用同一资源，KUN 会等待锁释放或过期后继续。"
                        ),
                    )
                )
            if conflicts:
                return ResourceLockAcquisition(
                    acquired=False,
                    holder_id=holder_id,
                    conflicts=conflicts,
                )
            acquired = [
                ResourceLockLease(
                    lease_id=f"lease-{_slug(holder_id)}-{_slug(resource_ref)}",
                    resource_ref=resource_ref,
                    holder_id=holder_id,
                    daemon_id=daemon_id,
                    worker_id=worker_id,
                    mission_id=work_item.mission_id,
                    work_item_id=work_item.work_item_id,
                    acquired_at=now,
                    expires_at=now + ttl,
                    heartbeat_at=now,
                )
                for resource_ref in unique_resources
            ]
            for lease in acquired:
                leases[lease.resource_ref] = lease
            self._persist_unlocked(list(leases.values()))
            return ResourceLockAcquisition(acquired=True, holder_id=holder_id, leases=acquired)

    def release_holder(self, holder_id: str, *, now: datetime) -> list[ResourceLockLease]:
        with self._locked():
            leases = self._load_unlocked(now=now)
            released = [
                lease for lease in leases.values() if lease.holder_id == holder_id
            ]
            remaining = [
                lease for lease in leases.values() if lease.holder_id != holder_id
            ]
            self._persist_unlocked(remaining)
            return released

    def list_active(self, *, now: datetime) -> list[ResourceLockLease]:
        with self._locked():
            leases = self._load_unlocked(now=now)
            self._persist_unlocked(list(leases.values()))
            return sorted(leases.values(), key=lambda lease: (lease.resource_ref, lease.holder_id))

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self, *, now: datetime) -> dict[str, ResourceLockLease]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        raw_leases = payload.get("leases", []) if isinstance(payload, dict) else []
        leases: dict[str, ResourceLockLease] = {}
        for raw_lease in raw_leases:
            lease = ResourceLockLease.model_validate(raw_lease)
            if lease.active(now=now):
                leases[lease.resource_ref] = lease
        return leases

    def _persist_unlocked(self, leases: list[ResourceLockLease]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "kun-v6-resource-lock-store-v1",
            "leases": [lease.model_dump(mode="json") for lease in leases],
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise


class InMemoryResourceLockStore:
    """Process-local lock store for unit tests and pure in-memory runtimes."""

    def __init__(self) -> None:
        self._leases: dict[str, ResourceLockLease] = {}

    def acquire_many(
        self,
        *,
        resources: list[str],
        holder_id: str,
        daemon_id: str,
        worker_id: str,
        work_item: WorkItem,
        now: datetime,
        ttl: timedelta,
    ) -> ResourceLockAcquisition:
        active = {
            resource_ref: lease
            for resource_ref, lease in self._leases.items()
            if lease.active(now=now)
        }
        conflicts: list[ResourceLockConflict] = []
        for resource_ref in _dedupe(resources):
            held = active.get(resource_ref)
            if held is None or held.holder_id == holder_id:
                continue
            conflicts.append(
                ResourceLockConflict(
                    resource_ref=resource_ref,
                    waiting_work_item_id=work_item.work_item_id,
                    holder_id=held.holder_id,
                    holder_work_item_id=held.work_item_id,
                    holder_daemon_id=held.daemon_id,
                    expires_at=held.expires_at,
                    waiting_reason="同一资源正在被其他 worker 使用。",
                )
            )
        if conflicts:
            self._leases = active
            return ResourceLockAcquisition(acquired=False, holder_id=holder_id, conflicts=conflicts)
        leases = [
            ResourceLockLease(
                lease_id=f"lease-{_slug(holder_id)}-{_slug(resource_ref)}",
                resource_ref=resource_ref,
                holder_id=holder_id,
                daemon_id=daemon_id,
                worker_id=worker_id,
                mission_id=work_item.mission_id,
                work_item_id=work_item.work_item_id,
                acquired_at=now,
                expires_at=now + ttl,
                heartbeat_at=now,
            )
            for resource_ref in _dedupe(resources)
        ]
        for lease in leases:
            active[lease.resource_ref] = lease
        self._leases = active
        return ResourceLockAcquisition(acquired=True, holder_id=holder_id, leases=leases)

    def release_holder(self, holder_id: str, *, now: datetime) -> list[ResourceLockLease]:
        active = {
            resource_ref: lease
            for resource_ref, lease in self._leases.items()
            if lease.active(now=now)
        }
        released = [lease for lease in active.values() if lease.holder_id == holder_id]
        self._leases = {
            resource_ref: lease
            for resource_ref, lease in active.items()
            if lease.holder_id != holder_id
        }
        return released

    def list_active(self, *, now: datetime) -> list[ResourceLockLease]:
        self._leases = {
            resource_ref: lease
            for resource_ref, lease in self._leases.items()
            if lease.active(now=now)
        }
        return sorted(self._leases.values(), key=lambda lease: (lease.resource_ref, lease.holder_id))


def worker_slots(config: WorkerPoolConfig) -> list[WorkerSlotSnapshot]:
    return [
        WorkerSlotSnapshot(
            slot_id=f"{config.pool_id}:{index + 1}",
            worker_id=f"{config.worker_id_prefix}-{index + 1}",
            machine_id=config.machine_id,
            status="idle",
        )
        for index in range(config.worker_count)
    ]


def sandbox_spec_for_work_item(
    *,
    work_item: WorkItem,
    mode: SandboxIsolationMode,
    workspace_ref: str | None,
    container_runtime: str | None,
) -> SandboxIsolationSpec:
    root_refs = [ref for ref in [workspace_ref, work_item.workspace_ref] if ref]
    if mode == "workspace_snapshot":
        text = "使用工作区边界、预执行快照和可审计回滚隔离本次执行。"
    elif mode == "container_required":
        text = "要求容器级隔离；runner 必须声明容器执行能力，否则应先阻断。"
    else:
        text = "使用外部容器或远端 worker 提供的隔离环境。"
    return SandboxIsolationSpec(
        sandbox_ref=work_item.sandbox_ref or f"sandbox://{work_item.mission_id}/{work_item.work_item_id}",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        mode=mode,
        workspace_ref=workspace_ref or work_item.workspace_ref,
        root_refs=_dedupe(root_refs),
        writable_refs=_dedupe(root_refs),
        container_runtime=container_runtime,
        checkpoint_refs=list(work_item.checkpoint_refs),
        rollback_refs=list(work_item.rollback_refs),
        text=text,
    )


def build_merge_governance_report(
    *,
    mission_id: str,
    work_item: WorkItem,
    artifacts: list[ArtifactRecord],
) -> MergeGovernanceReport:
    dependency_refs = list(work_item.dependencies)
    merged_artifact_refs = [artifact.artifact_id for artifact in artifacts]
    issues: list[MergeConflictIssue] = []
    if not dependency_refs:
        issues.append(
            MergeConflictIssue(
                severity="warning",
                code="merge_without_dependencies",
                summary="合并工作项没有显式依赖，可能只是空合并。",
                refs=[work_item.work_item_id],
            )
        )
    if not merged_artifact_refs and not work_item.recovery_refs:
        issues.append(
            MergeConflictIssue(
                severity="blocking",
                code="merge_inputs_missing",
                summary="没有可合并 artifact，也没有恢复引用，不能交付合并结果。",
                refs=[work_item.work_item_id],
            )
        )
    by_path: dict[str, list[str]] = {}
    for artifact in artifacts:
        if artifact.kind not in {"diff", "report", "answer"}:
            continue
        by_path.setdefault(artifact.path_or_uri, []).append(artifact.artifact_id)
    for path, refs in by_path.items():
        if len(refs) <= 1:
            continue
        issues.append(
            MergeConflictIssue(
                severity="blocking",
                code="overlapping_artifact_output",
                summary=f"多个上游产物写向同一输出位置：{path}",
                refs=refs,
            )
        )
    support_owners: dict[str, list[str]] = {}
    for artifact in artifacts:
        for support in artifact.supports:
            if support.startswith("writes:") or support.startswith("file:"):
                support_owners.setdefault(support, []).append(artifact.artifact_id)
    for support, refs in support_owners.items():
        if len(refs) > 1:
            issues.append(
                MergeConflictIssue(
                    severity="blocking",
                    code="overlapping_write_claim",
                    summary=f"多个 worker 声称修改同一资源：{support}",
                    refs=refs,
                )
            )
    pass_merge_gate = not any(issue.severity == "blocking" for issue in issues)
    return MergeGovernanceReport(
        mission_id=mission_id,
        work_item_id=work_item.work_item_id,
        dependency_refs=dependency_refs,
        merged_artifact_refs=merged_artifact_refs,
        issues=issues,
        pass_merge_gate=pass_merge_gate,
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:96] or "item"


__all__ = [
    "FileResourceLockStore",
    "InMemoryResourceLockStore",
    "MergeConflictIssue",
    "MergeGovernanceReport",
    "ResourceLockAcquisition",
    "ResourceLockConflict",
    "ResourceLockLease",
    "SandboxIsolationMode",
    "SandboxIsolationSpec",
    "WorkerPoolConfig",
    "WorkerSlotSnapshot",
    "WorkerSlotStatus",
    "build_merge_governance_report",
    "sandbox_spec_for_work_item",
    "worker_slots",
]
