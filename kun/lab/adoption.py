"""LabRecipeAdoption — idle_batch step 消费 experiment.promoted 事件 (Wire 23).

闭环关键: Wire 22 让 RecipePromoter emit experiment.promoted 进 events bus,
但主仓库还没人消费 — recipe 只在 outbox 里. Wire 23 加一个 idle_batch step,
每次 idle 周期拉新的 promoted 事件, 调 adopter callable 真用上.

数据流:
    KUN-Lab.RecipePromoter.promote_eligible
        → experiment.promoted event (events 表 + NATS)
        → idle_batch.LabRecipeAdoptionStep
        → adopter(payload)  ← 这里真改主仓库参数
                             (V2.2 §16.6 GuardPolicy 影子→canary→stable)

本 PR (Wire 23) 范围:
    - 数据流通: 拉事件 + 调 adopter + 记 adopted_at cursor
    - 默认 adopter: noop log (不真改主仓库参数, 留给 Wire 24)
    - 测试: 注入 fake adopter 验证 payload 透传
    - 幂等: in-memory cursor (last_adopted_at) 防同一 promotion 被消费多次

Wire 24 范围 (TODO):
    - adopter 真接 §16.6 Sediment / GuardPolicy
    - cursor 持久化 (lab_adopted_events 表 / Postgres advisory lock)
    - target_module 分发表: "execution_mode_classifier" 改 classifier 权重 etc.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.engineering.idle_batch import IdleBatchStep, register_step

logger = logging.getLogger(__name__)


# adopter signature: (promotion_payload) -> any awaitable.
# payload schema = LabEventEmitter.summarize_promotion 的输出
LabAdopter = Callable[[dict[str, Any]], Awaitable[None]]


async def _default_adopter(payload: dict[str, Any]) -> None:
    """默认 adopter: 只 log, 不改主仓库参数. Wire 24 替换成 §16.6 Sediment."""
    logger.info(
        "lab.recipe.adoption.default_noop",
        extra={
            "promotion_id": payload.get("promotion_id"),
            "task_type": payload.get("task_type"),
            "strategy": payload.get("strategy"),
            "win_rate": payload.get("win_rate"),
            "target_module": payload.get("target_module"),
        },
    )


@dataclass
class AdoptionState:
    """In-memory cursor — 防重复消费同一 promotion."""

    last_adopted_at: datetime = field(
        default_factory=lambda: datetime.fromtimestamp(0, tz=UTC)
    )
    adopted_promotion_ids: set[str] = field(default_factory=set)

    def mark(self, promotion_id: str, occurred_at: datetime) -> None:
        self.adopted_promotion_ids.add(promotion_id)
        if occurred_at > self.last_adopted_at:
            self.last_adopted_at = occurred_at


class LabRecipeAdoptionStep(IdleBatchStep):
    """idle_batch step: 拉新 experiment.promoted 事件 → adopter 处理.

    Args:
        adopter: async callable(payload_dict) → None. 默认 noop log.
                 真生产应注入 §16.6 Sediment / classifier-weight-tuner / etc.
        event_fetcher: async callable(*, event_type, since) → list[dict].
                       默认从 kun.core.db 拉 events 表; 测试可注入 in-memory list.
        max_per_cycle: 单次 idle_batch 最多消费几条 (防 noisy promote 占用 cycle).
    """

    step_id = "lab_recipe_adoption"

    def __init__(
        self,
        *,
        adopter: LabAdopter | None = None,
        event_fetcher: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
        max_per_cycle: int = 50,
    ) -> None:
        self._adopter = adopter or _default_adopter
        self._event_fetcher = event_fetcher
        self._max_per_cycle = max_per_cycle
        self._state = AdoptionState()

    @property
    def state(self) -> AdoptionState:
        return self._state

    def reset(self) -> None:
        """测试 / 重启用. 清掉 cursor."""
        self._state = AdoptionState()

    async def run(self, tenant_id: str) -> dict[str, Any]:
        events = await self._fetch_events(tenant_id)
        if not events:
            return {"scanned": 0, "adopted": 0, "skipped": 0, "errors": 0}

        events = events[: self._max_per_cycle]
        adopted = 0
        skipped = 0
        errors = 0

        for ev in events:
            payload = ev.get("payload") or {}
            promotion_id = str(payload.get("promotion_id", ""))
            occurred_at = ev.get("occurred_at") or datetime.now(UTC)

            if promotion_id and promotion_id in self._state.adopted_promotion_ids:
                skipped += 1
                continue

            try:
                await self._adopter(payload)
                if promotion_id:
                    self._state.mark(promotion_id, occurred_at)
                adopted += 1
            except Exception as e:
                logger.exception(
                    "lab.recipe.adoption.adopter_failed promotion=%s err=%s",
                    promotion_id,
                    e,
                )
                errors += 1

        logger.info(
            "lab.recipe.adoption.cycle_done tenant=%s scanned=%d adopted=%d skipped=%d errors=%d",
            tenant_id,
            len(events),
            adopted,
            skipped,
            errors,
        )
        return {
            "scanned": len(events),
            "adopted": adopted,
            "skipped": skipped,
            "errors": errors,
            "cursor": self._state.last_adopted_at.isoformat(),
        }

    async def _fetch_events(self, tenant_id: str) -> list[dict[str, Any]]:
        """拉 experiment.promoted 事件 since last cursor.

        默认: 从 kun.core.db.session_scope 拉 EventRow.
        测试: 注入 event_fetcher 返 in-memory list.
        """
        if self._event_fetcher is not None:
            return await self._event_fetcher(
                event_type="experiment.promoted",
                since=self._state.last_adopted_at,
                tenant_id=tenant_id,
            )

        # Best-effort 默认实现 (没 DB 时返空, 不爆)
        try:
            from sqlalchemy import select

            from kun.core.db import session_scope
            from kun.core.orm import EventRow

            async with session_scope() as s:
                stmt = (
                    select(EventRow)
                    .where(EventRow.event_type == "experiment.promoted")
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.occurred_at > self._state.last_adopted_at)
                    .order_by(EventRow.occurred_at)
                    .limit(self._max_per_cycle)
                )
                result = await s.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "event_id": r.event_id,
                        "occurred_at": r.occurred_at,
                        "payload": r.payload,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.debug("lab.recipe.adoption.fetch_skipped err=%s", e)
            return []


_step_singleton: LabRecipeAdoptionStep | None = None


def get_adoption_step() -> LabRecipeAdoptionStep:
    """单例. 让外部 (e.g. Wire 24 的 sediment 接口) 能拿到 state."""
    global _step_singleton
    if _step_singleton is None:
        _step_singleton = LabRecipeAdoptionStep()
    return _step_singleton


def install_lab_adoption_step(
    *,
    adopter: LabAdopter | None = None,
    event_fetcher: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
) -> LabRecipeAdoptionStep:
    """把 LabRecipeAdoptionStep 装进 idle_batch registry.

    应用启动时调一次 (e.g. install_runtime). 重复调会替换之前的 singleton.
    """
    global _step_singleton
    _step_singleton = LabRecipeAdoptionStep(adopter=adopter, event_fetcher=event_fetcher)
    register_step(_step_singleton)
    return _step_singleton


def reset_adoption_step() -> None:
    """测试用."""
    global _step_singleton
    _step_singleton = None


__all__ = [
    "AdoptionState",
    "LabAdopter",
    "LabRecipeAdoptionStep",
    "get_adoption_step",
    "install_lab_adoption_step",
    "reset_adoption_step",
]
