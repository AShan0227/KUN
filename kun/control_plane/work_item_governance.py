"""Work-item governance — locks / sandbox / worker slots / merge-governance for KUN V6.

⚠️ 2026-05-27 重命名自 `kun.control_plane.concurrency` (L1.4):
原名与 `kun.engineering.concurrency` (通用任务级并发原语) 命名冲突,
grep / 导航时混淆. 新名 `work_item_governance` 强调它的职责 — 不是
通用 concurrency, 是 control_plane 对**work-item 粒度**的治理 (worker
slot 配置 / sandbox 隔离 / 资源锁租约 / merge 冲突治理).

两者区别:
  kun.engineering.concurrency  → 任务级 IdempotencyKey / ResourceGuard / Lease
  kun.control_plane.work_item_governance → work-item 级 WorkerPoolConfig /
                                            ResourceLockLease / SandboxIsolationSpec /
                                            MergeGovernanceReport / 4 种 lock store

ADR-018 §16.5 ConcurrencySafety 半合并 — 两者实际是同概念域不同抽象层,
**不强行合并**, 用命名区分.

---

This module is the product boundary between "one daemon doing a batch" and a
real Control Plane that can coordinate multiple worker slots and multiple
daemon processes.  The first production implementation remains local-first, but
the contracts are intentionally durable: leases can live in a file/DB-backed
store, workers report slots, resource conflicts are auditable, sandbox mode is
explicit, and merge work can fail on concrete conflicts instead of blindly
attaching artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
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
ResourceLockBackend = Literal["file", "sqlite", "redis", "memory"]


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


def normalize_resource_lock_ref(value: str) -> str:
    """Canonicalize resource-lock keys before scheduling or persisting leases."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith("workspace:"):
        return raw
    workspace_value = raw.removeprefix("workspace:")
    if workspace_value.startswith("workspace://"):
        workspace_value = workspace_value.removeprefix("workspace://")
    if not workspace_value:
        return "workspace:"
    try:
        workspace_value = str(Path(workspace_value).expanduser().resolve())
    except (OSError, RuntimeError):
        workspace_value = str(Path(workspace_value).expanduser())
    return f"workspace:{workspace_value}"


WORKSPACE_LOCKING_WORK_ITEM_TYPES = frozenset(
    {"execution", "research", "review", "test", "merge", "repair", "retest", "rollback"}
)
WORKSPACE_RESOURCE_LOCK_PREFIXES = (
    "workspace:",
    "worktree:",
    "project:",
    "repo:",
    "output:",
    "output_dir:",
    "delivery:",
)


def is_pure_governance_work_item(work_item: WorkItem) -> bool:
    """Return whether a Qi/Nuo item should stay on the governance lane."""

    if work_item.type == "governance" and work_item.owner in {"qi", "nuo"}:
        return True
    if work_item.owner == "qi" and work_item.type == "research":
        text = " ".join(
            [
                work_item.work_item_id,
                work_item.idempotency_key or "",
                work_item.expected_output,
            ]
        ).lower()
        if (
            "strategy-replay" in text
            or "strategy replay" in text
            or "process audit" in text
            or "self_improvement" in text
            or "do not deliver user-task output" in text
        ):
            return True
    if work_item.owner != "nuo" or work_item.type != "repair":
        return False
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
        ]
    ).lower()
    return (
        "-observation-" in text
        or "runtime-observation:" in text
        or "classify this runtime observation" in text
    ) and "clean-retest" not in text


def is_workspace_resource_lock_ref(value: str) -> bool:
    """Return whether a resource lock protects workspace-like writable state."""

    normalized = normalize_resource_lock_ref(value)
    return normalized.startswith(WORKSPACE_RESOURCE_LOCK_PREFIXES)


def work_item_requires_workspace_boundary(work_item: WorkItem) -> bool:
    """Return whether this work item should lock and write the workspace."""

    if is_pure_governance_work_item(work_item):
        return False
    if work_item.type in WORKSPACE_LOCKING_WORK_ITEM_TYPES:
        return True
    return any(is_workspace_resource_lock_ref(value) for value in work_item.resource_locks)


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
        unique_resources = _dedupe_resource_refs(resources)
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
            released = [lease for lease in leases.values() if lease.holder_id == holder_id]
            remaining = [lease for lease in leases.values() if lease.holder_id != holder_id]
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


class SQLiteResourceLockStore:
    """SQLite-backed lock store for local multi-process worker pools.

    This is the durable default for a single machine running multiple daemon
    processes.  It uses SQLite's write transaction as the atomic boundary, so
    independently started daemon processes cannot double-claim the same
    workspace, mission merge lane, or other resource.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

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
        unique_resources = _dedupe_resource_refs(resources)
        if not unique_resources:
            return ResourceLockAcquisition(acquired=True, holder_id=holder_id)
        with self._transaction() as conn:
            self._prune_expired(conn, now=now)
            conflicts: list[ResourceLockConflict] = []
            for resource_ref in unique_resources:
                held = self._load_one(conn, resource_ref=resource_ref)
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
                            "另一个 daemon/worker 正在使用同一资源，KUN 会等待锁释放或过期后继续。"
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
            conn.executemany(
                """
                INSERT OR REPLACE INTO resource_locks
                    (resource_ref, holder_id, lease_json, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        lease.resource_ref,
                        lease.holder_id,
                        lease.model_dump_json(),
                        lease.expires_at.isoformat(),
                    )
                    for lease in acquired
                ],
            )
            return ResourceLockAcquisition(acquired=True, holder_id=holder_id, leases=acquired)

    def release_holder(self, holder_id: str, *, now: datetime) -> list[ResourceLockLease]:
        with self._transaction() as conn:
            self._prune_expired(conn, now=now)
            rows = conn.execute(
                "SELECT lease_json FROM resource_locks WHERE holder_id = ?",
                (holder_id,),
            ).fetchall()
            released = [ResourceLockLease.model_validate_json(str(row[0])) for row in rows]
            conn.execute(
                "DELETE FROM resource_locks WHERE holder_id = ?",
                (holder_id,),
            )
            return released

    def list_active(self, *, now: datetime) -> list[ResourceLockLease]:
        with self._transaction() as conn:
            self._prune_expired(conn, now=now)
            rows = conn.execute(
                "SELECT lease_json FROM resource_locks ORDER BY resource_ref"
            ).fetchall()
            return [ResourceLockLease.model_validate_json(str(row[0])) for row in rows]

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_locks (
                resource_ref TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                lease_json TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(resource_locks)").fetchall()
        }
        if "holder_id" not in columns:
            conn.execute("ALTER TABLE resource_locks ADD COLUMN holder_id TEXT NOT NULL DEFAULT ''")
            rows = conn.execute("SELECT resource_ref, lease_json FROM resource_locks").fetchall()
            for resource_ref, lease_json in rows:
                lease = ResourceLockLease.model_validate_json(str(lease_json))
                conn.execute(
                    "UPDATE resource_locks SET holder_id = ? WHERE resource_ref = ?",
                    (lease.holder_id, resource_ref),
                )
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _load_one(
        self,
        conn: sqlite3.Connection,
        *,
        resource_ref: str,
    ) -> ResourceLockLease | None:
        row = conn.execute(
            "SELECT lease_json FROM resource_locks WHERE resource_ref = ?",
            (resource_ref,),
        ).fetchone()
        if row is None:
            return None
        return ResourceLockLease.model_validate_json(str(row[0]))

    def _prune_expired(self, conn: sqlite3.Connection, *, now: datetime) -> None:
        conn.execute(
            "DELETE FROM resource_locks WHERE expires_at <= ?",
            (now.isoformat(),),
        )


class RedisResourceLockStore:
    """Redis-backed distributed lock store for cross-machine worker pools."""

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        client: object | None = None,
        key_prefix: str = "kun:v6:resource-lock",
    ) -> None:
        if client is None:
            if redis_url is None:
                raise ValueError("redis_url is required when client is not provided")
            import redis

            client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

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
        unique_resources = _dedupe_resource_refs(resources)
        if not unique_resources:
            return ResourceLockAcquisition(acquired=True, holder_id=holder_id)
        keys = [self._key(resource_ref) for resource_ref in unique_resources]
        ttl_ms = max(1, int(ttl.total_seconds() * 1000))
        while True:
            with self.client.pipeline() as pipe:  # type: ignore[attr-defined]
                try:
                    pipe.watch(*keys)
                    raw_values = pipe.mget(keys)
                    conflicts: list[ResourceLockConflict] = []
                    for resource_ref, raw_value in zip(
                        unique_resources,
                        raw_values,
                        strict=True,
                    ):
                        if not raw_value:
                            continue
                        held = ResourceLockLease.model_validate_json(str(raw_value))
                        if not held.active(now=now) or held.holder_id == holder_id:
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
                                    "Redis 分布式锁显示其他机器/worker 正在使用同一资源。"
                                ),
                            )
                        )
                    if conflicts:
                        pipe.unwatch()
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
                    pipe.multi()
                    for key, lease in zip(keys, acquired, strict=True):
                        pipe.set(key, lease.model_dump_json(), px=ttl_ms)
                    pipe.execute()
                    return ResourceLockAcquisition(
                        acquired=True,
                        holder_id=holder_id,
                        leases=acquired,
                    )
                except Exception as exc:
                    if exc.__class__.__name__ == "WatchError":
                        continue
                    raise

    def release_holder(self, holder_id: str, *, now: datetime) -> list[ResourceLockLease]:
        released: list[ResourceLockLease] = []
        for key in list(self.client.scan_iter(f"{self.key_prefix}:*")):  # type: ignore[attr-defined]
            raw_value = self.client.get(key)  # type: ignore[attr-defined]
            if not raw_value:
                continue
            lease = ResourceLockLease.model_validate_json(str(raw_value))
            if not lease.active(now=now):
                self.client.delete(key)  # type: ignore[attr-defined]
                continue
            if lease.holder_id != holder_id:
                continue
            released.append(lease)
            self.client.delete(key)  # type: ignore[attr-defined]
        return sorted(released, key=lambda lease: (lease.resource_ref, lease.holder_id))

    def list_active(self, *, now: datetime) -> list[ResourceLockLease]:
        leases: list[ResourceLockLease] = []
        for key in list(self.client.scan_iter(f"{self.key_prefix}:*")):  # type: ignore[attr-defined]
            raw_value = self.client.get(key)  # type: ignore[attr-defined]
            if not raw_value:
                continue
            lease = ResourceLockLease.model_validate_json(str(raw_value))
            if not lease.active(now=now):
                self.client.delete(key)  # type: ignore[attr-defined]
                continue
            leases.append(lease)
        return sorted(leases, key=lambda lease: (lease.resource_ref, lease.holder_id))

    def _key(self, resource_ref: str) -> str:
        digest = hashlib.sha256(resource_ref.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:{digest}"


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
        for resource_ref in _dedupe_resource_refs(resources):
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
            for resource_ref in _dedupe_resource_refs(resources)
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
        return sorted(
            self._leases.values(), key=lambda lease: (lease.resource_ref, lease.holder_id)
        )


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
    writable_refs = _dedupe(root_refs) if work_item_requires_workspace_boundary(work_item) else []
    if mode == "workspace_snapshot":
        text = "使用工作区边界、预执行快照和可审计回滚隔离本次执行。"
    elif mode == "container_required":
        text = "要求容器级隔离；runner 必须声明容器执行能力，否则应先阻断。"
    else:
        text = "使用外部容器或远端 worker 提供的隔离环境。"
    return SandboxIsolationSpec(
        sandbox_ref=work_item.sandbox_ref
        or f"sandbox://{work_item.mission_id}/{work_item.work_item_id}",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        mode=mode,
        workspace_ref=workspace_ref or work_item.workspace_ref,
        root_refs=_dedupe(root_refs),
        writable_refs=writable_refs,
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


def _dedupe_resource_refs(values: list[str]) -> list[str]:
    return _dedupe([normalize_resource_lock_ref(value) for value in values])


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:96] or "item"


__all__ = [
    "FileResourceLockStore",
    "InMemoryResourceLockStore",
    "MergeConflictIssue",
    "MergeGovernanceReport",
    "RedisResourceLockStore",
    "ResourceLockAcquisition",
    "ResourceLockBackend",
    "ResourceLockConflict",
    "ResourceLockLease",
    "SQLiteResourceLockStore",
    "SandboxIsolationMode",
    "SandboxIsolationSpec",
    "WorkerPoolConfig",
    "WorkerSlotSnapshot",
    "WorkerSlotStatus",
    "build_merge_governance_report",
    "is_pure_governance_work_item",
    "is_workspace_resource_lock_ref",
    "normalize_resource_lock_ref",
    "sandbox_spec_for_work_item",
    "work_item_requires_workspace_boundary",
    "worker_slots",
]
