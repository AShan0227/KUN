"""ConcurrencySafety (ADR-018 §16.5) — 统一并发安全机制.

合并前: 分布式锁 / 幂等键 / 版本号 / 冲突检测 / 预冲突扫描五种散在各处.
合并后: 统一在事前 + 事中入口处使用.

当前实装:
  - IdempotencyKey.check_or_record (Redis)
  - ResourceGuard.acquire / release (Redis distributed lock)
  - Version check 由 SQLAlchemy 乐观并发自动处理
  - 预冲突扫描 (pre-conflict scanner)
  - 动作前置队列 (pending-actions queue)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import redis.asyncio as aioredis
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.config import settings
from kun.core.ids import new_id
from kun.core.logging import get_logger
from kun.core.orm import PendingActionRow, RuntimeStateRow, TaskRow
from kun.datamodel.task import RiskLevel, TaskRef, TaskSpec
from kun.world.action_taxonomy import (
    TaxonomyResult,
    apply_taxonomy_audit_fields,
    normalize_world_action_type,
)

log = get_logger("kun.engineering.concurrency")


# =================== Idempotency (Redis SETNX) ===================


@dataclass(frozen=True)
class IdempotencyResult:
    first: bool
    cached_result_ref: str | None


def _sanitize_tenant(tenant_id: str) -> str:
    """Keep only safe chars in the Redis key segment so the prefix can't
    inject `:` or wildcards into the namespace."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", tenant_id)[:64] or "unknown"


class IdempotencyKey:
    """Redis-backed idempotency with TTL.

    Keys are namespaced per tenant so two tenants with colliding fingerprints
    can never see each other's cached result_ref.
    """

    def __init__(self, redis: Any, ttl_sec: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl_sec

    async def check_or_record(
        self,
        tenant_id: str,
        key: str,
        result_ref: str,
    ) -> IdempotencyResult:
        """Atomic 'record if not exists'. Returns whether this is first time."""
        full_key = f"kun:t:{_sanitize_tenant(tenant_id)}:idem:{key}"
        ok = await self._redis.set(full_key, result_ref, nx=True, ex=self._ttl)
        if ok:
            return IdempotencyResult(first=True, cached_result_ref=None)
        cached = await self._redis.get(full_key)
        return IdempotencyResult(first=False, cached_result_ref=cached)


# =================== Distributed lock (Redlock lite) ===================


@dataclass
class Lease:
    resource: str
    token: str
    ttl_sec: int
    tenant_id: str = "unknown"


class ResourceGuard:
    """Redis SET NX EX + token check on release — single-node lightweight lock.

    Locks are namespaced per tenant; two tenants asking for the same resource
    name (e.g. ``project:abc``) get independent locks.

    For production-grade Redlock, upgrade to redis-py's Redlock when multi-node.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def acquire(
        self,
        tenant_id: str,
        resource: str,
        *,
        ttl_sec: int = 10,
    ) -> Lease | None:
        token = uuid.uuid4().hex
        full_key = f"kun:t:{_sanitize_tenant(tenant_id)}:lock:{resource}"
        ok = await self._redis.set(full_key, token, nx=True, ex=ttl_sec)
        if not ok:
            return None
        return Lease(resource=resource, token=token, ttl_sec=ttl_sec, tenant_id=tenant_id)

    async def release(self, lease: Lease) -> bool:
        full_key = f"kun:t:{_sanitize_tenant(lease.tenant_id)}:lock:{lease.resource}"
        # Lua script: only delete if token matches (avoid releasing someone else's lock)
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        result = await self._redis.eval(script, 1, full_key, lease.token)
        return bool(result)


# =================== Convenience helpers ===================


_redis_pool: Any | None = None


async def _get_redis() -> Any:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(settings().redis_url, decode_responses=True)
    return _redis_pool


@asynccontextmanager
async def acquire_or_raise(
    tenant_id: str,
    resource: str,
    *,
    ttl_sec: int = 10,
) -> AsyncIterator[Lease]:
    """Grab a lock or raise. Auto-releases on exit. Per-tenant namespaced."""
    redis = await _get_redis()
    guard = ResourceGuard(redis)
    lease = await guard.acquire(tenant_id, resource, ttl_sec=ttl_sec)
    if lease is None:
        raise ResourceBusyError(resource)
    try:
        yield lease
    finally:
        await guard.release(lease)


class ResourceBusyError(RuntimeError):
    """Raised when a resource lock can't be acquired."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"resource busy: {resource}")
        self.resource = resource


# Backwards-compatible alias
ResourceBusy = ResourceBusyError


# =================== Pre-conflict scanner ===================


ResourceMode = Literal["read", "write"]

_ACTIVE_RUNTIME_STATUSES = ("queued", "running", "paused")
_SIDE_EFFECT_KEYWORDS = {
    "send": "email.draft",
    "email": "email.draft",
    "mail": "email.draft",
    "slack": "email.draft",
    "sms": "email.draft",
    "publish": "local_file.write",
    "post": "webhook.post_dry_run",
    "webhook": "webhook.post_dry_run",
    "api": "webhook.post_dry_run",
    "browser": "browser.plan",
    "click": "browser.plan",
    "form": "browser.plan",
    "delete": "resource.delete",
    "remove": "resource.delete",
    "transfer": "payment.transfer",
    "pay": "payment.transfer",
    "payment": "payment.transfer",
    "refund": "payment.refund",
    "deploy": "deployment.change",
    "merge": "repository.merge",
    "发送": "email.draft",
    "邮件": "email.draft",
    "发布": "local_file.write",
    "网页": "browser.plan",
    "浏览器": "browser.plan",
    "点击": "browser.plan",
    "表单": "browser.plan",
    "接口": "webhook.post_dry_run",
    "删除": "resource.delete",
    "转账": "payment.transfer",
    "支付": "payment.transfer",
    "退款": "payment.refund",
    "部署": "deployment.change",
    "合并": "repository.merge",
}
_EXPLICIT_ACTION_TYPE_RE = re.compile(
    r"(?<![a-z0-9])([a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+)(?![a-z0-9])"
)


class ResourceIntent(BaseModel):
    """A resource a task may touch before it starts running."""

    resource: str
    mode: ResourceMode = "read"
    reason: str = ""


class ConflictFinding(BaseModel):
    """One pre-start conflict with an already active task."""

    task_id: str
    status: str
    resource: str
    existing_mode: ResourceMode
    incoming_mode: ResourceMode
    reason: str = ""


class PreConflictReport(BaseModel):
    """Pre-start conflict scan result."""

    resources: list[ResourceIntent] = Field(default_factory=list)
    conflicts: list[ConflictFinding] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.conflicts)


class PendingActionSpec(BaseModel):
    """A side-effect action that must be approved before external execution."""

    action_id: str = Field(default_factory=lambda: new_id("action"))
    action_type: str
    target_ref: str = "unknown"
    risk_level: RiskLevel = "medium"
    payload: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        taxonomy = normalize_world_action_type(self.action_type, self.payload)
        self.action_type = taxonomy.action_type
        if "source_action_type" in self.payload and "taxonomy_reason" in self.payload:
            self.payload = {
                **self.payload,
                "matched_action_type": taxonomy.action_type,
            }
            return
        self.payload = apply_taxonomy_audit_fields(self.payload, taxonomy)


async def scan_pre_conflicts(
    session: AsyncSession,
    *,
    tenant_id: str,
    task_ref: TaskRef,
) -> PreConflictReport:
    """Scan active tasks for write conflicts before starting a new task."""
    incoming = derive_resource_intents(task_ref)
    if not incoming:
        return PreConflictReport()

    result = await session.execute(
        select(TaskRow, RuntimeStateRow.status)
        .join(RuntimeStateRow, RuntimeStateRow.task_ref == TaskRow.task_id)
        .where(TaskRow.tenant_id == tenant_id)
        .where(TaskRow.task_id != task_ref.meta.task_id)
        .where(RuntimeStateRow.status.in_(_ACTIVE_RUNTIME_STATUSES))
    )

    conflicts: list[ConflictFinding] = []
    for row in result.all():
        existing_task = cast(TaskRow, row[0])
        existing_status = cast(str, row[1])
        existing = _derive_resource_intents_from_task_row(existing_task)
        conflicts.extend(
            _compare_resource_intents(
                task_id=existing_task.task_id,
                status=existing_status,
                existing=existing,
                incoming=incoming,
            )
        )

    return PreConflictReport(resources=incoming, conflicts=conflicts)


async def scan_active_resource_conflicts(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> list[ConflictFinding]:
    """Scan already-active tasks for conflicting resource intents.

    `scan_pre_conflicts` protects the entrance. This function is for NUO:
    periodically ask “do we already have two active tasks fighting over the
    same resource?” so conflicts do not hide after a resume/rebase/manual edit.
    """
    result = await session.execute(
        select(TaskRow, RuntimeStateRow.status)
        .join(RuntimeStateRow, RuntimeStateRow.task_ref == TaskRow.task_id)
        .where(TaskRow.tenant_id == tenant_id)
        .where(RuntimeStateRow.status.in_(_ACTIVE_RUNTIME_STATUSES))
    )
    active = [
        (cast(TaskRow, row[0]), cast(str, row[1]), _derive_resource_intents_from_task_row(row[0]))
        for row in result.all()
    ]
    conflicts: list[ConflictFinding] = []
    for left_idx, (left_task, left_status, left_intents) in enumerate(active):
        for right_task, right_status, right_intents in active[left_idx + 1 :]:
            pair_conflicts = _compare_resource_intents(
                task_id=right_task.task_id,
                status=right_status,
                existing=left_intents,
                incoming=right_intents,
            )
            for conflict in pair_conflicts:
                if conflict.reason:
                    continue
                conflict.reason = (
                    f"active task {left_task.task_id} ({left_status}) conflicts "
                    f"with {right_task.task_id}"
                )
            conflicts.extend(pair_conflicts)
    return conflicts


def derive_resource_intents(task_ref: TaskRef) -> list[ResourceIntent]:
    """Derive conservative resource intents from TASK.md L1/L2."""
    intents: dict[str, ResourceIntent] = {}

    if task_ref.meta.owner.project_id:
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"project:{_normalize(task_ref.meta.owner.project_id)}",
                mode="write" if _task_has_side_effect(task_ref) else "read",
                reason="task owner project",
            ),
        )

    if task_ref.spec is not None:
        _add_spec_intents(intents, task_ref.spec)

    if _task_has_side_effect(task_ref):
        root = task_ref.meta.task_type.split(".", 1)[0]
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"side_effect:{_normalize(root)}",
                mode="write",
                reason="task appears to request an external side effect",
            ),
        )

    return sorted(intents.values(), key=lambda item: item.resource)


def pending_actions_for(task_ref: TaskRef) -> list[PendingActionSpec]:
    """Extract side-effect actions that should wait for approval."""
    text = _task_text(task_ref)
    taxonomies = _matched_action_taxonomies(text)
    if not taxonomies:
        return []

    target_ref = "unknown"
    if task_ref.spec and task_ref.spec.external_resources:
        target_ref = _target_ref(task_ref.spec.external_resources[0])
    elif task_ref.meta.owner.project_id:
        target_ref = f"project:{_normalize(task_ref.meta.owner.project_id)}"

    risk_level: RiskLevel = task_ref.meta.risk_level
    if risk_level == "low":
        risk_level = "medium"

    return [
        PendingActionSpec(
            action_type=taxonomy.action_type,
            target_ref=target_ref,
            risk_level=risk_level,
            payload=_pending_action_payload(
                task_ref=task_ref,
                taxonomy=taxonomy,
                target_ref=target_ref,
            ),
        )
        for taxonomy in sorted(taxonomies.values(), key=lambda item: item.action_type)
    ]


async def enqueue_pending_actions(
    session: AsyncSession,
    *,
    tenant_id: str,
    task_ref: TaskRef,
    actions: list[PendingActionSpec],
) -> None:
    """Persist pending side-effect actions in the same transaction as the task."""
    now = datetime.now(UTC)
    for action in actions:
        session.add(
            PendingActionRow(
                action_id=action.action_id,
                tenant_id=tenant_id,
                task_ref=task_ref.meta.task_id,
                action_type=action.action_type,
                target_ref=action.target_ref,
                status="pending_approval",
                risk_level=action.risk_level,
                payload=action.payload,
                created_at=now,
                updated_at=now,
            )
        )


def _derive_resource_intents_from_task_row(row: TaskRow) -> list[ResourceIntent]:
    owner_project = row.project_id
    spec = row.spec_json
    meta_text = " ".join(
        [
            row.task_type,
            row.success_criteria_short,
            _spec_text(spec),
        ]
    )
    has_side_effect = _has_side_effect_text(meta_text)

    intents: dict[str, ResourceIntent] = {}
    if owner_project:
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"project:{_normalize(owner_project)}",
                mode="write" if has_side_effect else "read",
                reason="existing task owner project",
            ),
        )
    if spec:
        _add_spec_dict_intents(intents, spec)
    if has_side_effect:
        root = row.task_type.split(".", 1)[0]
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"side_effect:{_normalize(root)}",
                mode="write",
                reason="existing task appears to request an external side effect",
            ),
        )
    return list(intents.values())


def _add_spec_intents(intents: dict[str, ResourceIntent], spec: TaskSpec) -> None:
    _add_tool_intents(intents, spec.required_tools)
    _add_external_resource_intents(intents, spec.external_resources)
    for constraint in spec.constraints:
        if constraint.kind == "path_only":
            _put_intent(
                intents,
                ResourceIntent(
                    resource=f"path:{_normalize(constraint.detail)}",
                    mode="write",
                    reason="path_only constraint",
                ),
            )


def _add_spec_dict_intents(intents: dict[str, ResourceIntent], spec: dict[str, Any]) -> None:
    _add_tool_intents(intents, [str(item) for item in spec.get("required_tools") or []])
    _add_external_resource_intents(
        intents,
        [str(item) for item in spec.get("external_resources") or []],
    )
    for raw_constraint in spec.get("constraints") or []:
        if not isinstance(raw_constraint, dict):
            continue
        if raw_constraint.get("kind") == "path_only":
            _put_intent(
                intents,
                ResourceIntent(
                    resource=f"path:{_normalize(str(raw_constraint.get('detail', 'unknown')))}",
                    mode="write",
                    reason="path_only constraint",
                ),
            )


def _add_tool_intents(intents: dict[str, ResourceIntent], tools: list[str]) -> None:
    for tool in tools:
        mode: ResourceMode = "write" if _has_side_effect_text(tool) else "read"
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"tool:{_normalize(tool)}",
                mode=mode,
                reason="required tool",
            ),
        )


def _add_external_resource_intents(
    intents: dict[str, ResourceIntent],
    resources: list[str],
) -> None:
    for resource in resources:
        mode: ResourceMode = "write" if _has_side_effect_text(resource) else "read"
        _put_intent(
            intents,
            ResourceIntent(
                resource=f"external:{_normalize(resource)}",
                mode=mode,
                reason="external resource",
            ),
        )


def _compare_resource_intents(
    *,
    task_id: str,
    status: str,
    existing: list[ResourceIntent],
    incoming: list[ResourceIntent],
) -> list[ConflictFinding]:
    conflicts: list[ConflictFinding] = []
    existing_by_resource = {item.resource: item for item in existing}
    for incoming_item in incoming:
        existing_item = existing_by_resource.get(incoming_item.resource)
        if existing_item is None:
            continue
        if existing_item.mode == "read" and incoming_item.mode == "read":
            continue
        conflicts.append(
            ConflictFinding(
                task_id=task_id,
                status=status,
                resource=incoming_item.resource,
                existing_mode=existing_item.mode,
                incoming_mode=incoming_item.mode,
                reason=incoming_item.reason or existing_item.reason,
            )
        )
    return conflicts


def _put_intent(intents: dict[str, ResourceIntent], intent: ResourceIntent) -> None:
    existing = intents.get(intent.resource)
    if existing is None or (existing.mode == "read" and intent.mode == "write"):
        intents[intent.resource] = intent


def _task_has_side_effect(task_ref: TaskRef) -> bool:
    return _has_side_effect_text(_task_text(task_ref))


def _task_text(task_ref: TaskRef) -> str:
    parts = [
        task_ref.meta.task_type,
        task_ref.meta.success_criteria_short,
    ]
    if task_ref.spec is not None:
        parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.required_tools),
                " ".join(task_ref.spec.external_resources),
                " ".join(task_ref.spec.success_metrics),
            ]
        )
    return " ".join(parts).lower()


def _spec_text(spec: dict[str, Any] | None) -> str:
    if not spec:
        return ""
    chunks: list[str] = []
    for key in ("goal_detail", "required_tools", "external_resources", "success_metrics"):
        value = spec.get(key)
        if isinstance(value, list):
            chunks.extend(str(item) for item in value)
        elif value is not None:
            chunks.append(str(value))
    return " ".join(chunks).lower()


def _has_side_effect_text(text: str) -> bool:
    return bool(_matched_action_types(text))


def _matched_action_types(text: str) -> set[str]:
    return set(_matched_action_taxonomies(text))


def _matched_action_taxonomies(text: str) -> dict[str, TaxonomyResult]:
    normalized = text.lower()
    explicit_action_types = _EXPLICIT_ACTION_TYPE_RE.findall(normalized)
    keyword_search_source = _EXPLICIT_ACTION_TYPE_RE.sub(" ", normalized)
    ascii_search_text = re.sub(r"[_\-.]+", " ", keyword_search_source)
    matched: dict[str, TaxonomyResult] = {}
    for explicit_action_type in explicit_action_types:
        taxonomy = normalize_world_action_type(explicit_action_type)
        if taxonomy.taxonomy_reason != "no_taxonomy_mapping_found":
            matched.setdefault(taxonomy.action_type, taxonomy)
    for keyword, action_type in _SIDE_EFFECT_KEYWORDS.items():
        if keyword.isascii():
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            if re.search(pattern, ascii_search_text):
                taxonomy = normalize_world_action_type(action_type)
                matched.setdefault(taxonomy.action_type, taxonomy)
        elif keyword in normalized:
            taxonomy = normalize_world_action_type(action_type)
            matched.setdefault(taxonomy.action_type, taxonomy)
    return matched


def _normalize(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff._:-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "unknown"


def _target_ref(value: str) -> str:
    raw = value.strip()
    if re.match(r"https?://", raw, flags=re.IGNORECASE):
        return raw
    if "@" in raw and not raw.startswith("project:"):
        return raw
    return _normalize(raw)


def _pending_action_payload(
    *,
    task_ref: TaskRef,
    taxonomy: TaxonomyResult,
    target_ref: str,
) -> dict[str, Any]:
    action_type = taxonomy.action_type
    base = {
        "task_id": task_ref.meta.task_id,
        "task_type": task_ref.meta.task_type,
        "success_criteria_short": task_ref.meta.success_criteria_short,
        "matched_action_type": action_type,
    }
    base = apply_taxonomy_audit_fields(base, taxonomy)
    goal = task_ref.spec.goal_detail if task_ref.spec else task_ref.meta.success_criteria_short
    if action_type == "email.draft":
        return {
            **base,
            "to": "" if target_ref.startswith("project:") else target_ref,
            "subject": task_ref.meta.success_criteria_short[:120],
            "body": goal,
        }
    if action_type == "local_file.write":
        safe_name = _normalize(task_ref.meta.task_id or "task")
        return {
            **base,
            "path": f"drafts/{safe_name}.md",
            "content": goal,
        }
    if action_type == "webhook.post_dry_run":
        return {
            **base,
            "url": target_ref if target_ref.startswith(("http://", "https://")) else "",
            "json": {
                "task_id": task_ref.meta.task_id,
                "summary": task_ref.meta.success_criteria_short,
                "goal": goal,
            },
        }
    if action_type == "browser.plan":
        return {
            **base,
            "url": target_ref if target_ref.startswith(("http://", "https://")) else "",
            "objective": goal,
            "steps": [],
        }
    return base
