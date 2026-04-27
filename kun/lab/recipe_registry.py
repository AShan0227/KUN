"""LabRecipeRegistry — lab 推过来的有效 recipe 给主仓库消费 (Wire 25).

Wire 24 把 lab promotion 经 KP 转成 AssetUpdate, 但 KP 的 _asset_apply_hook
默认没人接 — AssetUpdate 只进 audit log, 没真改主仓库行为. Wire 25:

    1. LabRecipeRegistry: in-memory 持久化 (task_type, target_module) → recipe
       带 last_updated/win_rate/strategy 的 mapping
    2. make_registry_apply_hook(registry): 包成 KP._asset_apply_hook 的注入点
    3. ExecutionMode classifier 在 default_mode fallback 之前查 registry,
       lab 推荐 strategy 含 "max" / "fast" 关键字时影响 mode

存储为 in-memory dict (Wire 26 接 DB). 进程重启清零 — lab 重新跑会重新推.

防御:
    - registry 只存 confidence 高的 (默认 ≥0.7), 防 lab 噪声污染主决策
    - registry 只对 task_type 给 hint, 不强制覆盖 risk_level=critical / 显式
      force_mode (那些规则优先级高于 lab 推荐)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from kun.engineering.precipitation import AssetUpdate

logger = logging.getLogger(__name__)


MIN_CONFIDENCE_FOR_REGISTRY = 0.7


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass
class LabRecipeEntry:
    """单条 lab 推过来的 recipe — registry 里的一行."""

    task_type: str
    target_module: str
    strategy: str
    win_rate: float
    confidence: float
    promotion_id: str = ""
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    extras: dict[str, Any] = field(default_factory=dict)


class LabRecipeStorage(Protocol):
    async def load_all(self, tenant_id: str) -> list[LabRecipeEntry]: ...
    async def save(self, tenant_id: str, entry: LabRecipeEntry) -> None: ...
    async def clear(self, tenant_id: str) -> None: ...


class InMemoryLabRecipeStorage:
    """Small async storage used by tests and as a no-DB fallback."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], LabRecipeEntry] = {}

    async def load_all(self, tenant_id: str) -> list[LabRecipeEntry]:
        return [
            entry
            for (stored_tenant, _task_type, _target), entry in self._store.items()
            if stored_tenant == tenant_id
        ]

    async def save(self, tenant_id: str, entry: LabRecipeEntry) -> None:
        self._store[(tenant_id, entry.task_type, entry.target_module)] = entry

    async def clear(self, tenant_id: str) -> None:
        for key in [key for key in self._store if key[0] == tenant_id]:
            del self._store[key]


class SqlLabRecipeStorage:
    """SQLAlchemy storage for lab_recipe_registry.

    The schema is managed by alembic 0013_lab_recipe_registry.
    """

    def __init__(self, session_factory: Callable[..., Any] | None = None) -> None:
        self._session_factory = session_factory

    def _open_session(self, tenant_id: str) -> Any:
        if self._session_factory is not None:
            return self._session_factory(tenant_id=tenant_id)
        from kun.core.db import session_scope

        return session_scope(tenant_id=tenant_id)

    async def load_all(self, tenant_id: str) -> list[LabRecipeEntry]:
        from sqlalchemy import text

        async with self._open_session(tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT task_type, target_module, strategy, win_rate, confidence, "
                    "promotion_id, extras, last_updated "
                    "FROM lab_recipe_registry WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
            rows = result.all()
        return [
            LabRecipeEntry(
                task_type=row.task_type,
                target_module=row.target_module,
                strategy=row.strategy,
                win_rate=float(row.win_rate),
                confidence=float(row.confidence),
                promotion_id=row.promotion_id or "",
                last_updated=row.last_updated,
                extras=dict(row.extras or {}),
            )
            for row in rows
        ]

    async def save(self, tenant_id: str, entry: LabRecipeEntry) -> None:
        from sqlalchemy import text

        async with self._open_session(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO lab_recipe_registry "
                    "(tenant_id, task_type, target_module, strategy, win_rate, confidence, "
                    "promotion_id, extras, last_updated) "
                    "VALUES (:tenant_id, :task_type, :target_module, :strategy, :win_rate, "
                    ":confidence, :promotion_id, CAST(:extras AS JSONB), :last_updated) "
                    "ON CONFLICT (tenant_id, task_type, target_module) DO UPDATE SET "
                    "strategy = EXCLUDED.strategy, "
                    "win_rate = EXCLUDED.win_rate, "
                    "confidence = EXCLUDED.confidence, "
                    "promotion_id = EXCLUDED.promotion_id, "
                    "extras = EXCLUDED.extras, "
                    "last_updated = EXCLUDED.last_updated"
                ),
                {
                    "tenant_id": tenant_id,
                    "task_type": entry.task_type,
                    "target_module": entry.target_module,
                    "strategy": entry.strategy,
                    "win_rate": entry.win_rate,
                    "confidence": entry.confidence,
                    "promotion_id": entry.promotion_id,
                    "extras": _json_dumps(entry.extras),
                    "last_updated": entry.last_updated,
                },
            )

    async def clear(self, tenant_id: str) -> None:
        from sqlalchemy import text

        async with self._open_session(tenant_id) as session:
            await session.execute(
                text("DELETE FROM lab_recipe_registry WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )


class LabRecipeRegistry:
    """In-memory 持久化 lab 推过来的有效 recipe.

    主仓库 ExecutionMode classifier / hermes prompt builder / etc. 查这里
    决定行为.

    线程安全: 单进程 asyncio 不需要锁 (idle_batch + classifier 都在主 loop).
    """

    def __init__(
        self,
        *,
        min_confidence: float = MIN_CONFIDENCE_FOR_REGISTRY,
        tenant_id: str = "u-sylvan",
        storage: LabRecipeStorage | None = None,
    ) -> None:
        self._entries: dict[tuple[str, str], LabRecipeEntry] = {}
        self._min_confidence = min_confidence
        self._tenant_id = tenant_id
        self._storage = storage

    def upsert(self, entry: LabRecipeEntry) -> bool:
        """加 / 更新一条 recipe. confidence 不够 → 拒绝, 返 False."""
        if entry.confidence < self._min_confidence:
            logger.debug(
                "lab.registry.rejected_low_confidence task=%s strat=%s conf=%.2f<%.2f",
                entry.task_type,
                entry.strategy,
                entry.confidence,
                self._min_confidence,
            )
            return False
        key = (entry.task_type, entry.target_module)
        self._entries[key] = entry
        logger.info(
            "lab.registry.upsert task=%s target=%s strat=%s win_rate=%.2f",
            entry.task_type,
            entry.target_module,
            entry.strategy,
            entry.win_rate,
        )
        # Wire 28: gauge 更新 (best-effort)
        try:
            from kun.core.metrics import lab_registry_size

            lab_registry_size.set(len(self._entries))
        except Exception as exc:
            logger.debug("lab.registry.metric_skipped err=%s", exc)
        return True

    async def aupsert(self, entry: LabRecipeEntry, *, tenant_id: str | None = None) -> bool:
        ok = self.upsert(entry)
        if ok and self._storage is not None:
            await self._storage.save(tenant_id or self._tenant_id, entry)
        return ok

    async def load_from_storage(self, *, tenant_id: str | None = None) -> int:
        if self._storage is None:
            return 0
        loaded = await self._storage.load_all(tenant_id or self._tenant_id)
        for entry in loaded:
            if entry.confidence >= self._min_confidence:
                self._entries[(entry.task_type, entry.target_module)] = entry
        return len(loaded)

    def get(self, task_type: str, target_module: str) -> LabRecipeEntry | None:
        return self._entries.get((task_type, target_module))

    def by_task_type(self, task_type: str) -> list[LabRecipeEntry]:
        return [e for (tt, _), e in self._entries.items() if tt == task_type]

    def all(self) -> list[LabRecipeEntry]:
        return list(self._entries.values())

    def clear(self) -> None:
        self._entries.clear()

    async def aclear(self, *, tenant_id: str | None = None) -> None:
        self.clear()
        if self._storage is not None:
            await self._storage.clear(tenant_id or self._tenant_id)

    def __len__(self) -> int:
        return len(self._entries)


def make_registry_apply_hook(
    registry: LabRecipeRegistry,
) -> Callable[[AssetUpdate], Awaitable[None]]:
    """包装 registry → KP._asset_apply_hook callable.

    用法 (主仓库 install_runtime 里):
        registry = LabRecipeRegistry()
        kp.register_asset_apply_hook(make_registry_apply_hook(registry))
        app.state.lab_recipe_registry = registry
    """

    async def apply_hook(update: AssetUpdate) -> None:
        # 只接 lab 来源的 update — 其他 (e.g. capability_card stats_writeback) 跳过
        if update.payload.get("source") != "kun_lab":
            return
        task_type = str(update.payload.get("task_type") or "")
        target = str(update.asset_ref or "")
        if not task_type or not target:
            logger.debug(
                "lab.registry.hook_skip_no_task_target update=%s",
                update.update_id,
            )
            return
        entry = LabRecipeEntry(
            task_type=task_type,
            target_module=target,
            strategy=str(update.payload.get("strategy") or ""),
            win_rate=float(update.payload.get("win_rate") or 0.0),
            confidence=update.confidence,
            promotion_id=str(update.payload.get("promotion_id") or ""),
            extras={
                "total_count": update.payload.get("total_count"),
                "avg_score": update.payload.get("avg_score"),
                "avg_cost_usd": update.payload.get("avg_cost_usd"),
                "requires_approval": update.requires_approval,
            },
        )
        await registry.aupsert(
            entry,
            tenant_id=str(update.payload.get("tenant_id") or "u-sylvan"),
        )

    return apply_hook


_registry_singleton: LabRecipeRegistry | None = None


def get_recipe_registry(
    *,
    storage: LabRecipeStorage | None = None,
    tenant_id: str = "u-sylvan",
) -> LabRecipeRegistry:
    """单例. 主仓库 ExecutionMode classifier / hermes 等都拉这个."""
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = LabRecipeRegistry(storage=storage, tenant_id=tenant_id)
    return _registry_singleton


def reset_recipe_registry() -> None:
    global _registry_singleton
    _registry_singleton = None


__all__ = [
    "MIN_CONFIDENCE_FOR_REGISTRY",
    "InMemoryLabRecipeStorage",
    "LabRecipeEntry",
    "LabRecipeRegistry",
    "LabRecipeStorage",
    "SqlLabRecipeStorage",
    "get_recipe_registry",
    "make_registry_apply_hook",
    "reset_recipe_registry",
]
