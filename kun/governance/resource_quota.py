"""Resource Quota — token / time / dedup cooldown 工程化限流 (L4.5).

防 RSI 闭环资源爆炸 — Pool 多实例 + 候选并行可能让单 tenant 在短时
间内消耗大量 token / 触发大量实验. 工程化护栏:

  1. Token budget: per-tenant 滑动窗口累计 LLM token, 超 → 暂停 RSI
  2. Experiment budget: per-tenant 窗口内最多 N 个 active experiment
  3. Dedup_key cooldown: 复用 Supervisor 的 dedup TTL (已实装)

工程化优先 — 命中 quota → 拒绝新 experiment + log 警告 + 等下个窗口.
不调 LLM. 全 in-memory state (production 用 Redis).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.resource_quota")


DEFAULT_TOKEN_BUDGET_PER_HOUR = 1_000_000
"""单 tenant 每小时 LLM token 预算上限. 超 → 拒绝新 experiment."""

DEFAULT_EXPERIMENT_BUDGET_PER_HOUR = 30
"""单 tenant 每小时最多 active experiment 数."""

DEFAULT_WINDOW_SECONDS = 3600
"""滑动窗口大小 (默认 1 小时)."""


@dataclass
class _TokenRecord:
    timestamp: datetime
    tokens: int


@dataclass
class _ExperimentRecord:
    timestamp: datetime
    experiment_id: str


@dataclass
class QuotaState:
    """Per-tenant in-memory quota state."""

    tenant_id: str
    token_records: deque = field(default_factory=lambda: deque())
    experiment_records: deque = field(default_factory=lambda: deque())

    def purge_expired(self, now: datetime, window_seconds: int) -> None:
        cutoff = now - timedelta(seconds=window_seconds)
        while self.token_records and self.token_records[0].timestamp < cutoff:
            self.token_records.popleft()
        while (
            self.experiment_records
            and self.experiment_records[0].timestamp < cutoff
        ):
            self.experiment_records.popleft()

    def current_token_usage(self) -> int:
        return sum(r.tokens for r in self.token_records)

    def current_experiment_count(self) -> int:
        return len(self.experiment_records)


@dataclass(frozen=True)
class QuotaCheckResult:
    """单次 quota 检查结果."""

    allowed: bool
    reason: str
    current_token_usage: int = 0
    current_experiment_count: int = 0
    token_budget: int = 0
    experiment_budget: int = 0


class ResourceQuota:
    """工程化限流 — per-tenant token + experiment budget.

    入口:
      check_and_record_experiment(tenant_id, estimated_tokens) →
        QuotaCheckResult; allowed=False 时拒绝新 experiment 入库.
      record_token_usage(tenant_id, tokens) → 显式累记 (LLM call 后).

    State per-tenant, asyncio.Lock 保护并发.
    """

    def __init__(
        self,
        *,
        token_budget_per_window: int = DEFAULT_TOKEN_BUDGET_PER_HOUR,
        experiment_budget_per_window: int = DEFAULT_EXPERIMENT_BUDGET_PER_HOUR,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        if token_budget_per_window < 0:
            raise ValueError("token_budget must be >= 0")
        if experiment_budget_per_window < 0:
            raise ValueError("experiment_budget must be >= 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._token_budget = token_budget_per_window
        self._experiment_budget = experiment_budget_per_window
        self._window_seconds = window_seconds
        self._states: dict[str, QuotaState] = defaultdict(
            lambda: QuotaState(tenant_id="")
        )
        self._lock = asyncio.Lock()

    def _state_for(self, tenant_id: str) -> QuotaState:
        state = self._states[tenant_id]
        if not state.tenant_id:
            # 第一次访问 — 设上 tenant_id
            state.tenant_id = tenant_id
        return state

    async def check_and_record_experiment(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        estimated_tokens: int = 0,
    ) -> QuotaCheckResult:
        """检查 tenant 是否可新增 experiment + 显式预扣 token.

        allowed=False → caller 拒绝 experiment 入库 + 不消耗 token.
        """
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)

            current_tokens = state.current_token_usage()
            current_experiments = state.current_experiment_count()

            if current_experiments >= self._experiment_budget:
                return QuotaCheckResult(
                    allowed=False,
                    reason=(
                        f"experiment_budget_exceeded: "
                        f"current={current_experiments} >= "
                        f"budget={self._experiment_budget}"
                    ),
                    current_token_usage=current_tokens,
                    current_experiment_count=current_experiments,
                    token_budget=self._token_budget,
                    experiment_budget=self._experiment_budget,
                )

            if current_tokens + estimated_tokens > self._token_budget:
                return QuotaCheckResult(
                    allowed=False,
                    reason=(
                        f"token_budget_exceeded: "
                        f"current+estimated={current_tokens + estimated_tokens} > "
                        f"budget={self._token_budget}"
                    ),
                    current_token_usage=current_tokens,
                    current_experiment_count=current_experiments,
                    token_budget=self._token_budget,
                    experiment_budget=self._experiment_budget,
                )

            # 通过 — 记录 experiment + 预扣 token
            state.experiment_records.append(
                _ExperimentRecord(timestamp=now, experiment_id=experiment_id)
            )
            if estimated_tokens > 0:
                state.token_records.append(
                    _TokenRecord(timestamp=now, tokens=estimated_tokens)
                )

            return QuotaCheckResult(
                allowed=True,
                reason="ok",
                current_token_usage=current_tokens + estimated_tokens,
                current_experiment_count=current_experiments + 1,
                token_budget=self._token_budget,
                experiment_budget=self._experiment_budget,
            )

    async def record_token_usage(
        self,
        tenant_id: str,
        tokens: int,
    ) -> None:
        """显式累记 token 用量 (LLM call 完成后)."""
        if tokens <= 0:
            return
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)
            state.token_records.append(_TokenRecord(timestamp=now, tokens=tokens))

    async def snapshot(self, tenant_id: str) -> dict[str, Any]:
        """读 quota 当前状态 (调试 / monitoring)."""
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)
            return {
                "tenant_id": tenant_id,
                "current_token_usage": state.current_token_usage(),
                "token_budget": self._token_budget,
                "current_experiment_count": state.current_experiment_count(),
                "experiment_budget": self._experiment_budget,
                "window_seconds": self._window_seconds,
            }


__all__ = [
    "DEFAULT_EXPERIMENT_BUDGET_PER_HOUR",
    "DEFAULT_TOKEN_BUDGET_PER_HOUR",
    "DEFAULT_WINDOW_SECONDS",
    "QuotaCheckResult",
    "QuotaState",
    "ResourceQuota",
]
