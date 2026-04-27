"""V2.3 Wire 39 — ProtocolRegistry: KUN 协议核心 (V2.3 §3).

跟 V2.2 LabRecipeRegistry 不同:
- LabRecipeRegistry: (task_type, target_module) → 单 strategy
- ProtocolRegistry: protocol_id × version × tenant, 完整任务执行模板

协议 (Protocol) lifecycle:
  experimental → shadow (旁路) → canary (5% 流量) → stable (100%)
                                                 ↓ rolled_back

启 export protocol → 经过 shadow/canary 验证 → 鲲 load stable 版本.

"协议是 KUN 的 IP" — 启反复探索沉淀的"鲲怎么干活的标准说明书".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


ProtocolStatus = Literal["experimental", "shadow", "canary", "stable", "rolled_back"]


class ProtocolTrigger(BaseModel):
    """什么 task 触发用这个 protocol."""

    model_config = ConfigDict(extra="forbid")

    task_type_pattern: str = Field(description="glob, e.g. 'writing.creative.*'")
    complexity_score_min: float = 0.0
    complexity_score_max: float = 1.0
    risk_levels: list[str] = Field(default_factory=lambda: ["low", "medium", "high", "critical"])


class ProtocolExecution(BaseModel):
    """执行参数."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "SMART"  # FAST/SMART/MAX/ENSEMBLE
    llm_strategy: str = ""  # tier_top_low_temp / chain_of_thought / etc.
    max_steps: int = 5
    expected_cost_usd: float = 0.05
    expected_duration_sec: float = 30.0


class ProtocolSkillStep(BaseModel):
    """skill_chain 单步."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    when: str = "always"  # condition (free text, evaluated by LLM/rules)
    timeout_sec: int = 60
    fallback: str = ""


class ProtocolHermesTemplate(BaseModel):
    """hermes prompt 注入."""

    model_config = ConfigDict(extra="forbid")

    system_prompt_addon: str = ""
    action_type_preference: list[str] = Field(default_factory=list)


class ProtocolVerificationSpec(BaseModel):
    """verification 规格 (跟 kun.datamodel.verification_spec.VerificationSpec 一致)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    spec: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class Protocol(BaseModel):
    """完整协议 — KUN 怎么干特定 task 的标准说明书."""

    model_config = ConfigDict(extra="forbid")

    protocol_id: str = Field(description="e.g. 'writing.creative.short_form'")
    version: str = Field(description="semantic, e.g. '1.2.0'")
    tenant_id: str = "u-sylvan"
    status: ProtocolStatus = "experimental"

    trigger: ProtocolTrigger
    execution: ProtocolExecution = Field(default_factory=ProtocolExecution)
    skill_chain: list[ProtocolSkillStep] = Field(default_factory=list)
    hermes_template: ProtocolHermesTemplate = Field(default_factory=ProtocolHermesTemplate)
    verification: list[ProtocolVerificationSpec] = Field(default_factory=list)

    # 6 维 reward 本协议的权重 (V2.2 §25 联动)
    reward_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "cost": 0.15,
            "latency": 0.10,
            "quality": 0.40,
            "user_satisfaction": 0.20,
            "reuse_potential": 0.10,
            "contribution": 0.05,
        }
    )

    # A/B pairing — 跟 challenger 协议对比
    a_b_pairing: dict[str, Any] = Field(default_factory=dict)

    # lifecycle metadata
    created_by: str = "qi"  # qi / user / claude / codex
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    promoted_at: datetime | None = None
    rollback_at: datetime | None = None
    rollback_reason: str = ""

    metadata: dict[str, Any] = Field(default_factory=dict)

    def matches(self, task_meta: dict[str, Any]) -> bool:
        """task_meta 是否触发该协议."""
        import fnmatch

        task_type = str(task_meta.get("task_type", ""))
        if not fnmatch.fnmatch(task_type, self.trigger.task_type_pattern):
            return False
        complexity = float(task_meta.get("complexity_score", 0.5))
        if complexity < self.trigger.complexity_score_min:
            return False
        if complexity > self.trigger.complexity_score_max:
            return False
        risk = str(task_meta.get("risk_level", "low"))
        return risk in self.trigger.risk_levels


class ProtocolStorage(Protocol):
    """协议存取 — InMemory + SQL 双实现."""

    async def save(self, protocol: Protocol) -> None: ...
    async def get(self, tenant_id: str, protocol_id: str, version: str) -> Protocol | None: ...
    async def get_active(
        self, tenant_id: str, protocol_id: str, status: ProtocolStatus = "stable"
    ) -> Protocol | None: ...
    async def list_all(self, tenant_id: str) -> list[Protocol]: ...
    async def update_status(
        self,
        tenant_id: str,
        protocol_id: str,
        version: str,
        new_status: ProtocolStatus,
        rollback_reason: str = "",
    ) -> None: ...


class InMemoryProtocolStorage:
    """默认 storage. 单元测试 / 没 DB 时用."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], Protocol] = {}

    async def save(self, protocol: Protocol) -> None:
        key = (protocol.tenant_id, protocol.protocol_id, protocol.version)
        self._store[key] = protocol.model_copy()

    async def get(self, tenant_id: str, protocol_id: str, version: str) -> Protocol | None:
        return self._store.get((tenant_id, protocol_id, version))

    async def get_active(
        self,
        tenant_id: str,
        protocol_id: str,
        status: ProtocolStatus = "stable",
    ) -> Protocol | None:
        candidates = [
            p
            for (t, pid, _), p in self._store.items()
            if t == tenant_id and pid == protocol_id and p.status == status
        ]
        if not candidates:
            return None
        # 多个同 status → 返最新 promoted (或 created)
        candidates.sort(key=lambda p: p.promoted_at or p.created_at, reverse=True)
        return candidates[0]

    async def list_all(self, tenant_id: str) -> list[Protocol]:
        return [p for (t, _, _), p in self._store.items() if t == tenant_id]

    async def update_status(
        self,
        tenant_id: str,
        protocol_id: str,
        version: str,
        new_status: ProtocolStatus,
        rollback_reason: str = "",
    ) -> None:
        key = (tenant_id, protocol_id, version)
        if key not in self._store:
            return
        p = self._store[key]
        new_protocol = p.model_copy(update={"status": new_status})
        if new_status == "stable":
            new_protocol.promoted_at = datetime.now(UTC)
        if new_status == "rolled_back":
            new_protocol.rollback_at = datetime.now(UTC)
            new_protocol.rollback_reason = rollback_reason
        self._store[key] = new_protocol

    def reset(self) -> None:
        self._store.clear()


SessionFactory = Callable[..., Any]


class SqlProtocolStorage:
    """SQLAlchemy 后端. 表由 alembic 0015_protocols 管理."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory

    def _open(self, tenant_id: str) -> Any:
        if self._session_factory is not None:
            return self._session_factory(tenant_id=tenant_id)
        from kun.core.db import session_scope

        return session_scope(tenant_id=tenant_id)

    async def save(self, protocol: Protocol) -> None:
        from sqlalchemy import text

        async with self._open(protocol.tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO protocols (
                        tenant_id, protocol_id, version, status, content,
                        created_by, created_at, promoted_at, rollback_at, rollback_reason
                    )
                    VALUES (
                        :tenant_id, :protocol_id, :version, :status,
                        CAST(:content AS JSONB),
                        :created_by, :created_at, :promoted_at, :rollback_at, :rollback_reason
                    )
                    ON CONFLICT (tenant_id, protocol_id, version) DO UPDATE SET
                        status = EXCLUDED.status,
                        content = EXCLUDED.content,
                        promoted_at = EXCLUDED.promoted_at,
                        rollback_at = EXCLUDED.rollback_at,
                        rollback_reason = EXCLUDED.rollback_reason
                    """
                ),
                {
                    "tenant_id": protocol.tenant_id,
                    "protocol_id": protocol.protocol_id,
                    "version": protocol.version,
                    "status": protocol.status,
                    "content": json.dumps(protocol.model_dump(mode="json", exclude={"tenant_id"})),
                    "created_by": protocol.created_by,
                    "created_at": protocol.created_at,
                    "promoted_at": protocol.promoted_at,
                    "rollback_at": protocol.rollback_at,
                    "rollback_reason": protocol.rollback_reason,
                },
            )

    async def get(self, tenant_id: str, protocol_id: str, version: str) -> Protocol | None:
        from sqlalchemy import text

        async with self._open(tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT content FROM protocols "
                    "WHERE tenant_id=:t AND protocol_id=:pid AND version=:v"
                ),
                {"t": tenant_id, "pid": protocol_id, "v": version},
            )
            row = result.first()
        if row is None:
            return None
        content = row[0]
        if isinstance(content, str):
            content = json.loads(content)
        content["tenant_id"] = tenant_id
        return Protocol.model_validate(content)

    async def get_active(
        self,
        tenant_id: str,
        protocol_id: str,
        status: ProtocolStatus = "stable",
    ) -> Protocol | None:
        from sqlalchemy import text

        async with self._open(tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT content FROM protocols "
                    "WHERE tenant_id=:t AND protocol_id=:pid AND status=:s "
                    "ORDER BY COALESCE(promoted_at, created_at) DESC LIMIT 1"
                ),
                {"t": tenant_id, "pid": protocol_id, "s": status},
            )
            row = result.first()
        if row is None:
            return None
        content = row[0]
        if isinstance(content, str):
            content = json.loads(content)
        content["tenant_id"] = tenant_id
        return Protocol.model_validate(content)

    async def list_all(self, tenant_id: str) -> list[Protocol]:
        from sqlalchemy import text

        async with self._open(tenant_id) as session:
            result = await session.execute(
                text("SELECT content FROM protocols WHERE tenant_id=:t"),
                {"t": tenant_id},
            )
            rows = result.all()
        protocols = []
        for row in rows:
            content = row[0]
            if isinstance(content, str):
                content = json.loads(content)
            content["tenant_id"] = tenant_id
            protocols.append(Protocol.model_validate(content))
        return protocols

    async def update_status(
        self,
        tenant_id: str,
        protocol_id: str,
        version: str,
        new_status: ProtocolStatus,
        rollback_reason: str = "",
    ) -> None:
        from sqlalchemy import text

        now = datetime.now(UTC)
        async with self._open(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE protocols SET status=:s, "
                    "promoted_at=CASE WHEN :s='stable' THEN :now ELSE promoted_at END, "
                    "rollback_at=CASE WHEN :s='rolled_back' THEN :now ELSE rollback_at END, "
                    "rollback_reason=CASE WHEN :s='rolled_back' THEN :reason ELSE rollback_reason END "
                    "WHERE tenant_id=:t AND protocol_id=:pid AND version=:v"
                ),
                {
                    "s": new_status,
                    "now": now,
                    "reason": rollback_reason,
                    "t": tenant_id,
                    "pid": protocol_id,
                    "v": version,
                },
            )


class ProtocolRegistry:
    """协议注册表. 鲲 load stable, 启 write experimental.

    用法:
        reg = get_protocol_registry()
        active = await reg.find_protocol_for(task_meta, tenant_id)
        if active:
            apply_protocol(active, task_meta)
    """

    def __init__(self, storage: ProtocolStorage | None = None) -> None:
        self._storage = storage or InMemoryProtocolStorage()
        # in-memory cache for fast lookup
        self._cache: dict[tuple[str, str], Protocol] = {}

    async def save(self, protocol: Protocol) -> None:
        await self._storage.save(protocol)
        if protocol.status == "stable":
            self._cache[(protocol.tenant_id, protocol.protocol_id)] = protocol

    async def get_active(
        self,
        tenant_id: str,
        protocol_id: str,
        status: ProtocolStatus = "stable",
    ) -> Protocol | None:
        if status == "stable":
            cached = self._cache.get((tenant_id, protocol_id))
            if cached is not None:
                return cached
        protocol = await self._storage.get_active(tenant_id, protocol_id, status)
        if protocol is not None and status == "stable":
            self._cache[(tenant_id, protocol_id)] = protocol
        return protocol

    async def find_protocol_for(self, task_meta: dict[str, Any], tenant_id: str) -> Protocol | None:
        """根据 task_meta 找匹配的 stable 协议. 没匹配 → None (走默认行为)."""
        all_protocols = await self._storage.list_all(tenant_id)
        candidates = [p for p in all_protocols if p.status == "stable" and p.matches(task_meta)]
        if not candidates:
            return None
        # 多个匹配 → 返最 specific (pattern 字符长的优先, e.g. "writing.creative.short" > "writing.*")
        candidates.sort(key=lambda p: len(p.trigger.task_type_pattern), reverse=True)
        return candidates[0]

    async def promote(
        self,
        tenant_id: str,
        protocol_id: str,
        version: str,
        target_status: ProtocolStatus,
    ) -> None:
        """状态迁移: experimental → shadow → canary → stable."""
        valid_transitions = {
            "experimental": ["shadow", "rolled_back"],
            "shadow": ["canary", "rolled_back"],
            "canary": ["stable", "rolled_back"],
            "stable": ["rolled_back"],
            "rolled_back": [],
        }
        current = await self._storage.get(tenant_id, protocol_id, version)
        if current is None:
            raise ValueError(f"Protocol {protocol_id}@{version} not found for {tenant_id}")
        if target_status not in valid_transitions.get(current.status, []):
            raise ValueError(
                f"Invalid transition: {current.status} → {target_status} for {protocol_id}@{version}"
            )
        await self._storage.update_status(tenant_id, protocol_id, version, target_status)
        # invalidate cache
        self._cache.pop((tenant_id, protocol_id), None)

    async def rollback(
        self,
        tenant_id: str,
        protocol_id: str,
        version: str,
        *,
        reason: str = "",
    ) -> None:
        """回退到 rolled_back 状态. 需要外部决定切到哪个老 stable 版本."""
        await self._storage.update_status(
            tenant_id, protocol_id, version, "rolled_back", rollback_reason=reason
        )
        self._cache.pop((tenant_id, protocol_id), None)

    async def list_all(self, tenant_id: str) -> list[Protocol]:
        return await self._storage.list_all(tenant_id)


_registry_singleton: ProtocolRegistry | None = None


def get_protocol_registry() -> ProtocolRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ProtocolRegistry()
    return _registry_singleton


def reset_protocol_registry() -> None:
    """测试用."""
    global _registry_singleton
    _registry_singleton = None


__all__ = [
    "InMemoryProtocolStorage",
    "Protocol",
    "ProtocolExecution",
    "ProtocolHermesTemplate",
    "ProtocolRegistry",
    "ProtocolSkillStep",
    "ProtocolStatus",
    "ProtocolStorage",
    "ProtocolTrigger",
    "ProtocolVerificationSpec",
    "SqlProtocolStorage",
    "get_protocol_registry",
    "reset_protocol_registry",
]
