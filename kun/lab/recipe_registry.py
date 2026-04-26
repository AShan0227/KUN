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

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kun.engineering.precipitation import AssetUpdate

logger = logging.getLogger(__name__)


MIN_CONFIDENCE_FOR_REGISTRY = 0.7


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


class LabRecipeRegistry:
    """In-memory 持久化 lab 推过来的有效 recipe.

    主仓库 ExecutionMode classifier / hermes prompt builder / etc. 查这里
    决定行为.

    线程安全: 单进程 asyncio 不需要锁 (idle_batch + classifier 都在主 loop).
    """

    def __init__(self, *, min_confidence: float = MIN_CONFIDENCE_FOR_REGISTRY) -> None:
        self._entries: dict[tuple[str, str], LabRecipeEntry] = {}
        self._min_confidence = min_confidence

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
        return True

    def get(self, task_type: str, target_module: str) -> LabRecipeEntry | None:
        return self._entries.get((task_type, target_module))

    def by_task_type(self, task_type: str) -> list[LabRecipeEntry]:
        return [e for (tt, _), e in self._entries.items() if tt == task_type]

    def all(self) -> list[LabRecipeEntry]:
        return list(self._entries.values())

    def clear(self) -> None:
        self._entries.clear()

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
        registry.upsert(entry)

    return apply_hook


_registry_singleton: LabRecipeRegistry | None = None


def get_recipe_registry() -> LabRecipeRegistry:
    """单例. 主仓库 ExecutionMode classifier / hermes 等都拉这个."""
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = LabRecipeRegistry()
    return _registry_singleton


def reset_recipe_registry() -> None:
    global _registry_singleton
    _registry_singleton = None


__all__ = [
    "MIN_CONFIDENCE_FOR_REGISTRY",
    "LabRecipeEntry",
    "LabRecipeRegistry",
    "get_recipe_registry",
    "make_registry_apply_hook",
    "reset_recipe_registry",
]
