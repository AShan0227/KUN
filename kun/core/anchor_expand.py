"""Anchor-Then-Expand iterator (V2.2 §19.3 — KUN 决策核心 B).

通用按需扩展模式. 任何"集合检索"操作 (拉记忆 / 列候选 / 选 skill / 跑 judge /
扫信息源 / 列诊断发现) 都不一次返 N 个, 而是流式返:
1. 第 1 轮: 返 1 个最可能的 "锚点" (anchor)
2. 调用方判断"够吗?" — 够就 break, 不够进入第 2 轮
3. 第 2 轮: 沿着锚点的关系/相似性/邻接扩展第 2-3 个
4. 总轮数 ≤ max_rounds (默认 3), 超过强制停

用法:
    async def my_anchor():
        return get_top_1(query)

    async def my_expand(anchor, prior):
        # prior 包含 anchor + 之前 yield 出的所有
        return find_next_relevant(anchor, prior)

    async for item in AnchorExpandIterator(my_anchor, my_expand, max_rounds=3):
        process(item)
        if my_caller_satisfied: break

可选配 marginal_roi 自动停止:
    criterion = MarginalROIStopCriterion(...)
    async for item in AnchorExpandIterator(
        anchor_fn, expand_fn, max_rounds=3,
        stop_criterion=criterion,
        value_estimator=ValueEstimator(strategy="cumulative_quality"),
    ):
        process(item)  # criterion 自动判断, 不需要调用方手动 break

设计原则:
- 流式 (AsyncIterator), 不是 list — 调用方决定何时 break
- max_rounds 强约束 (1-10), 不允许无限
- expand_fn 返 None → 提前结束 (没更多了)
- 异常容错 (anchor_fn / expand_fn 抛出时 yield 已有的 + log)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from kun.engineering.marginal_roi import MarginalROIStopCriterion, ValueEstimator

logger = logging.getLogger(__name__)


@dataclass
class ExpansionStats:
    """扩展过程的统计 (后续 watchtower / capability_card 学习用)."""

    rounds_completed: int = 0
    items_yielded: int = 0
    stopped_reason: str = ""  # "max_rounds" / "expand_returned_none" / "marginal_stop" / "exception" / "still_running"
    value_history: list[float] = field(default_factory=list)


class AnchorExpandIterator[T]:
    """通用按需扩展迭代器.

    Args:
        anchor_fn: async () -> T. 第一轮的锚点产生函数.
        expand_fn: async (anchor: T, prior_items: list[T]) -> T | None.
                   扩展函数, 返 None 表示没更多了.
        max_rounds: 最大轮数 (含 anchor 那一轮). 默认 3.
        stop_criterion: 可选 MarginalROIStopCriterion, 自动判断停止.
        value_estimator: 可选 ValueEstimator, 配合 stop_criterion 用.
        on_done: 可选 callable[[ExpansionStats], None], 完结时回调 (用于学习).

    Note:
        max_rounds 含 anchor 在内. max_rounds=1 → 只 yield anchor.
        max_rounds=3 → 最多 anchor + 2 个 expand.
    """

    def __init__(
        self,
        anchor_fn: Callable[[], Awaitable[T]],
        expand_fn: Callable[[T, list[T]], Awaitable[T | None]],
        *,
        max_rounds: int = 3,
        stop_criterion: MarginalROIStopCriterion | None = None,
        value_estimator: ValueEstimator | None = None,
        on_done: Callable[[ExpansionStats], None] | None = None,
    ) -> None:
        if max_rounds < 1 or max_rounds > 10:
            raise ValueError(f"max_rounds must be in [1, 10], got {max_rounds}")
        if stop_criterion is not None and value_estimator is None:
            raise ValueError("stop_criterion requires value_estimator")
        self.anchor_fn = anchor_fn
        self.expand_fn = expand_fn
        self.max_rounds = max_rounds
        self.stop_criterion = stop_criterion
        self.value_estimator = value_estimator
        self.on_done = on_done
        self.stats = ExpansionStats()

    async def __aiter__(self) -> AsyncIterator[T]:
        prior: list[T] = []

        # Round 1: anchor
        try:
            anchor = await self.anchor_fn()
        except Exception as e:
            logger.exception("anchor_fn failed")
            self.stats.stopped_reason = f"anchor_exception:{e}"
            self._finish()
            return

        prior.append(anchor)
        self.stats.rounds_completed = 1
        self.stats.items_yielded = 1
        if self.value_estimator is not None:
            v = self.value_estimator.estimate(anchor, [])
            self.stats.value_history.append(v)
        yield anchor

        # 检查 marginal stop (anchor 之后)
        if self._should_marginal_stop():
            self.stats.stopped_reason = "marginal_stop"
            self._finish()
            return

        # Round 2..max_rounds: expand
        for round_idx in range(2, self.max_rounds + 1):
            try:
                next_item = await self.expand_fn(anchor, list(prior))
            except Exception as e:
                logger.exception("expand_fn failed at round %d", round_idx)
                self.stats.stopped_reason = f"expand_exception:{e}"
                self._finish()
                return

            if next_item is None:
                self.stats.stopped_reason = "expand_returned_none"
                self._finish()
                return

            self.stats.rounds_completed = round_idx
            self.stats.items_yielded += 1
            if self.value_estimator is not None:
                v = self.value_estimator.estimate(next_item, list(prior))
                self.stats.value_history.append(v)
            prior.append(next_item)
            yield next_item

            # 检查 marginal stop (本轮之后)
            if self._should_marginal_stop():
                self.stats.stopped_reason = "marginal_stop"
                self._finish()
                return

        self.stats.stopped_reason = "max_rounds"
        self._finish()

    def _should_marginal_stop(self) -> bool:
        if self.stop_criterion is None:
            return False
        decision = self.stop_criterion.should_stop(self.stats.value_history)
        return decision.should_stop

    def _finish(self) -> None:
        if self.on_done is not None:
            try:
                self.on_done(self.stats)
            except Exception:
                logger.exception("on_done callback failed")


# ============================================================================
# Helpers — 常见集成模式
# ============================================================================


async def collect_all[T](iterator: AnchorExpandIterator[T]) -> list[T]:
    """把 iterator 收集成 list (测试用 / 不需要流式时方便).

    注意: 跑完整 max_rounds, 失去流式优势. 仅在不需要按需停的场景用.
    """
    items: list[T] = []
    async for item in iterator:
        items.append(item)
    return items


async def collect_until[T](
    iterator: AnchorExpandIterator[T],
    predicate: Callable[[list[T]], bool],
) -> list[T]:
    """收集直到 predicate(prior) 返 True, 然后停.

    适合"够用就停"的场景, 比手写 break 简洁.
    """
    items: list[T] = []
    async for item in iterator:
        items.append(item)
        if predicate(items):
            break
    return items


__all__ = [
    "AnchorExpandIterator",
    "ExpansionStats",
    "collect_all",
    "collect_until",
]
