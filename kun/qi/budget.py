"""启 V3 日预算守门 (V2.3 §4.2).

跟 Wire 27 EnsembleConfig.cost_budget_total_usd (单实验级) 不同,
这是日级总预算 — 启每天累计 cost 超 SoulFile.qi_daily_budget_usd → 暂停.

防"启探索失控烧爆账单".
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


class QiBudgetExhaustedError(RuntimeError):
    """启日预算耗尽."""


class QiDailyBudget:
    """启日预算追踪 — in-memory, 跨 process 不共享 (M5 接 DB).

    设计简单: 按日累加 cost. 超 budget 抛.
    重启清零 (符合"启每天独立预算"的语义).

    用法:
        budget = get_qi_budget()
        budget.set_daily_limit(5.0)
        budget.add_cost("user-id", 0.05)  # 抛 if 超
    """

    def __init__(self) -> None:
        # (user_id, date) → accumulated_cost
        self._costs: dict[tuple[str, date], float] = defaultdict(float)
        self._daily_limit: float = 5.0  # default $5/day

    def set_daily_limit(self, limit_usd: float) -> None:
        if limit_usd < 0:
            raise ValueError("daily_limit must be >= 0")
        self._daily_limit = limit_usd

    def get_today_spent(self, user_id: str, *, today: date | None = None) -> float:
        today = today or datetime.now(UTC).date()
        return self._costs[(user_id, today)]

    def remaining_budget(self, user_id: str, *, today: date | None = None) -> float:
        return max(0.0, self._daily_limit - self.get_today_spent(user_id, today=today))

    def add_cost(self, user_id: str, cost_usd: float, *, today: date | None = None) -> float:
        """加 cost. 加完后超 budget → raise QiBudgetExhaustedError.

        Returns: 加完后的 today_spent.
        """
        today = today or datetime.now(UTC).date()
        new_total = self._costs[(user_id, today)] + cost_usd
        if new_total > self._daily_limit:
            logger.warning(
                "qi.budget_exhausted user=%s today_spent=%.4f limit=%.4f cost=%.4f",
                user_id,
                self._costs[(user_id, today)],
                self._daily_limit,
                cost_usd,
            )
            raise QiBudgetExhaustedError(
                f"启日预算耗尽: user={user_id} today_spent=${self._costs[(user_id, today)]:.4f} "
                f"+ this_cost=${cost_usd:.4f} > limit=${self._daily_limit:.4f}. "
                "明日重置, 或调高 SoulFile.qi_daily_budget_usd."
            )
        self._costs[(user_id, today)] = new_total
        return new_total

    def reset(self) -> None:
        """测试用. 清所有计数."""
        self._costs.clear()


_budget_singleton: QiDailyBudget | None = None


def get_qi_budget() -> QiDailyBudget:
    """单例 — 跨启 module 共享."""
    global _budget_singleton
    if _budget_singleton is None:
        _budget_singleton = QiDailyBudget()
    return _budget_singleton


def reset_qi_budget() -> None:
    """测试用."""
    global _budget_singleton
    _budget_singleton = None
