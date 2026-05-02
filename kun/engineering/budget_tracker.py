"""BudgetTracker — 预算追踪四档 (V2.1 §5.2 / T18 / V1 §5.2 完整版).

V1 §5.2 定义了四档但 quota_tracker 是另一回事 (5h 滚动配额, 不是预算).
V2.1 加完整四档预算 + 自动收敛 + 摘要替换历史 + 硬熔断.

| 档位 | 剩余预算 | 行为 |
|------|---------|------|
| HIGH | > 50% | 正常探索 (无限制) |
| MEDIUM | 20-50% | 保守 (优先稳定方案) |
| LOW | 5-20% | 收敛 (仅已验证路径) |
| CRITICAL | < 5% | 自动用摘要替换历史 / 询问追加预算 |
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)


BudgetLevel = Literal["HIGH", "MEDIUM", "LOW", "CRITICAL"]
BudgetScope = Literal["task", "user_daily", "user_monthly", "global_daily"]


@dataclass
class BudgetState:
    """单个预算实例."""

    scope: BudgetScope
    scope_id: str  # task_id / user_id / 'global'
    limit_usd: float
    used_usd: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def remaining_ratio(self) -> float:
        if self.limit_usd <= 0:
            return 1.0
        return max(0.0, 1.0 - (self.used_usd / self.limit_usd))

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.used_usd)

    @property
    def level(self) -> BudgetLevel:
        ratio = self.remaining_ratio
        if ratio > 0.50:
            return "HIGH"
        if ratio > 0.20:
            return "MEDIUM"
        if ratio > 0.05:
            return "LOW"
        return "CRITICAL"


# 各档行为
BEHAVIOR_BY_LEVEL: dict[BudgetLevel, dict[str, Any]] = {
    "HIGH": {
        "exploration": "normal",
        "model_tier_pref": None,  # 无限制
        "summary_replace_history": False,
        "request_topup": False,
    },
    "MEDIUM": {
        "exploration": "conservative",
        "model_tier_pref": "stable",
        "summary_replace_history": False,
        "request_topup": False,
    },
    "LOW": {
        "exploration": "converge_verified_only",
        "model_tier_pref": "cheap_first",
        "summary_replace_history": False,
        "request_topup": False,
    },
    "CRITICAL": {
        "exploration": "halt",
        "model_tier_pref": "cheap_only",
        "summary_replace_history": True,  # 自动摘要历史
        "request_topup": True,  # 问用户追加预算
    },
}

# 硬熔断阈值
HARD_BREAK_RATIO_TASK = 1.20  # 单任务 × 1.2 → 强制停
HARD_BREAK_RATIO_GLOBAL = 1.10  # 全局日 × 1.1 → 排队


class BudgetTracker:
    """预算追踪四档收敛."""

    def __init__(self) -> None:
        self._budgets: dict[tuple[BudgetScope, str], BudgetState] = {}
        self._listeners: list[Callable[[str, BudgetState], None]] = []

    def register_budget(
        self,
        scope: BudgetScope,
        scope_id: str,
        limit_usd: float,
    ) -> BudgetState:
        st = BudgetState(scope=scope, scope_id=scope_id, limit_usd=limit_usd)
        self._budgets[(scope, scope_id)] = st
        return st

    def register_listener(
        self,
        fn: Callable[[str, BudgetState], None],
    ) -> None:
        """监听 level 变化 / 硬熔断."""
        self._listeners.append(fn)

    def _emit(self, kind: str, state: BudgetState) -> None:
        for fn in self._listeners:
            try:
                fn(kind, state)
            except Exception:
                logger.exception("budget listener failed")

    def consume(
        self,
        scope: BudgetScope,
        scope_id: str,
        amount_usd: float,
    ) -> BudgetLevel:
        """消费预算. 返当前 level. 触发 level 变化时 emit."""
        st = self._budgets.get((scope, scope_id))
        if st is None:
            # 没注册 → 默认无限预算
            return "HIGH"

        old_level = st.level
        st.used_usd += amount_usd
        st.last_updated_at = datetime.now(UTC)
        new_level = st.level

        if new_level != old_level:
            self._emit(f"level_change_{new_level}", st)

        # 硬熔断检查
        usage_ratio = st.used_usd / max(st.limit_usd, 1e-6)
        if scope == "task" and usage_ratio >= HARD_BREAK_RATIO_TASK:
            self._emit("hard_break_task", st)
        elif scope == "global_daily" and usage_ratio >= HARD_BREAK_RATIO_GLOBAL:
            self._emit("hard_break_global", st)

        return new_level

    def get_state(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> BudgetState | None:
        return self._budgets.get((scope, scope_id))

    def get_behavior(self, level: BudgetLevel) -> dict[str, Any]:
        return dict(BEHAVIOR_BY_LEVEL[level])

    def should_summarize_history(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> bool:
        """CRITICAL 档自动摘要替换历史."""
        st = self.get_state(scope, scope_id)
        if st is None:
            return False
        return BEHAVIOR_BY_LEVEL[st.level]["summary_replace_history"]  # type: ignore[no-any-return]

    def should_request_topup(
        self,
        scope: BudgetScope,
        scope_id: str,
    ) -> bool:
        """CRITICAL 档问用户追加预算."""
        st = self.get_state(scope, scope_id)
        if st is None:
            return False
        return BEHAVIOR_BY_LEVEL[st.level]["request_topup"]  # type: ignore[no-any-return]

    def get_dashboard(self, user_id: str) -> dict[str, Any]:
        """NUO 第 1 层预算面板."""
        # 找该 user 相关的所有 scope
        relevant = [
            st
            for (scope, sid), st in self._budgets.items()
            if sid == user_id or scope == "global_daily"
        ]
        return {
            "user_id": user_id,
            "budgets": [
                {
                    "scope": st.scope,
                    "scope_id": st.scope_id,
                    "limit_usd": st.limit_usd,
                    "used_usd": st.used_usd,
                    "remaining_usd": st.remaining_usd,
                    "level": st.level,
                    "behavior": BEHAVIOR_BY_LEVEL[st.level],
                }
                for st in relevant
            ],
        }


__all__ = [
    "BEHAVIOR_BY_LEVEL",
    "HARD_BREAK_RATIO_GLOBAL",
    "HARD_BREAK_RATIO_TASK",
    "BudgetLevel",
    "BudgetScope",
    "BudgetState",
    "BudgetTracker",
]
