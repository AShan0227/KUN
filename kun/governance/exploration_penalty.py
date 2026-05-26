"""Exploration Penalty — 失败候选短期内不重复 + 相似策略合并 (L4.6).

防 RSI 闭环陷入"反复尝试同一失败策略"的死循环:
  - 同 signature 的 candidate 短期内失败 ≥ MAX_RETRIES_PER_WINDOW → 标 blocked
  - blocked 期间该 signature 的新 candidate 不进 emit (Strategist 端过滤)
  - 成功 → clear 该 signature 的 failure count
  - 与 L4.4 deliberation.deduplicate 协同: 那是"同批合并", 这是"跨时间避免"

工程化记忆 — per-tenant + per-signature 计数器, 滑动窗口 (默认 24h).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.agents.strategist.service import StrategyExperiment

log = get_logger("kun.governance.exploration_penalty")


DEFAULT_MAX_RETRIES_PER_WINDOW = 3
"""同 signature 失败 ≥ 此值 → blocked. ADR-024 §探索惩罚 默认 3 次."""

DEFAULT_PENALTY_WINDOW_SECONDS = 86400
"""窗口大小 24h — 超过窗口的失败记录被 purge."""


@dataclass
class _FailureRecord:
    timestamp: datetime
    signature: str
    experiment_id: str
    reason: str


@dataclass
class PenaltyState:
    """Per-tenant in-memory penalty state."""

    tenant_id: str
    failure_records: dict[str, deque] = field(
        default_factory=lambda: defaultdict(deque)
    )
    """signature → deque[_FailureRecord]"""

    def purge_expired(self, now: datetime, window_seconds: int) -> None:
        cutoff = now - timedelta(seconds=window_seconds)
        empty_keys: list[str] = []
        for sig, records in self.failure_records.items():
            while records and records[0].timestamp < cutoff:
                records.popleft()
            if not records:
                empty_keys.append(sig)
        for k in empty_keys:
            del self.failure_records[k]

    def failure_count(self, signature: str) -> int:
        return len(self.failure_records.get(signature, ()))


@dataclass(frozen=True)
class PenaltyCheckResult:
    """单次 penalty 检查结果."""

    blocked: bool
    signature: str
    current_failure_count: int
    max_retries: int
    reason: str = ""


def candidate_signature_key(c: StrategyExperiment) -> str:
    """从 StrategyExperiment 算 stable signature string.

    与 deliberation._candidate_signature 不同: 那个返回 set 用于 Jaccard;
    这里返回 sorted tuple 拼接的 stable string 用于精确 lookup.
    """
    parts = [
        f"target={c.target_module}",
        f"level={c.target_level}",
        f"kind={c.change_spec.get('kind', 'unknown')}",
        f"mode={c.explorer_mode}",
        f"rollout={c.rollout_mode}",
    ]
    # change_spec 的关键标识字段也加入 signature (但避免完整 dump)
    extras = sorted(
        f"{k}={v}"
        for k, v in c.change_spec.items()
        if k != "kind" and not isinstance(v, dict | list)
    )
    parts.extend(extras[:5])  # 限 top-5 避免 signature 过长
    return "|".join(parts)


class ExplorationPenalty:
    """工程化探索惩罚 — per-tenant 失败 signature 跟踪.

    入口:
      check(tenant_id, candidate) → PenaltyCheckResult, blocked=True 表
        失败次数已达上限, caller 拒绝 emit
      record_failure(tenant_id, candidate, experiment_id, reason) → 累记
      record_success(tenant_id, signature) → 清该 signature 计数
      filter_candidates(tenant_id, candidates) → 过滤掉 blocked 的
    """

    def __init__(
        self,
        *,
        max_retries_per_window: int = DEFAULT_MAX_RETRIES_PER_WINDOW,
        window_seconds: int = DEFAULT_PENALTY_WINDOW_SECONDS,
    ) -> None:
        if max_retries_per_window < 1:
            raise ValueError("max_retries_per_window must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_retries = max_retries_per_window
        self._window_seconds = window_seconds
        self._states: dict[str, PenaltyState] = {}
        self._lock = asyncio.Lock()

    def _state_for(self, tenant_id: str) -> PenaltyState:
        if tenant_id not in self._states:
            self._states[tenant_id] = PenaltyState(tenant_id=tenant_id)
        return self._states[tenant_id]

    async def check(
        self,
        tenant_id: str,
        candidate: StrategyExperiment,
    ) -> PenaltyCheckResult:
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)
            sig = candidate_signature_key(candidate)
            count = state.failure_count(sig)
            if count >= self._max_retries:
                return PenaltyCheckResult(
                    blocked=True,
                    signature=sig,
                    current_failure_count=count,
                    max_retries=self._max_retries,
                    reason=(
                        f"signature failed {count} times in last "
                        f"{self._window_seconds}s (>= max_retries={self._max_retries})"
                    ),
                )
            return PenaltyCheckResult(
                blocked=False,
                signature=sig,
                current_failure_count=count,
                max_retries=self._max_retries,
            )

    async def record_failure(
        self,
        tenant_id: str,
        candidate: StrategyExperiment,
        *,
        experiment_id: str | None = None,
        reason: str = "unspecified",
    ) -> None:
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)
            sig = candidate_signature_key(candidate)
            state.failure_records[sig].append(
                _FailureRecord(
                    timestamp=now,
                    signature=sig,
                    experiment_id=experiment_id or candidate.experiment_id,
                    reason=reason,
                )
            )
            log.info(
                "exploration_penalty.failure_recorded",
                tenant_id=tenant_id,
                signature=sig,
                count=state.failure_count(sig),
            )

    async def record_success(
        self,
        tenant_id: str,
        candidate: StrategyExperiment,
    ) -> None:
        """清该 signature 的 failure 记录 — 成功 → 重新可探."""
        async with self._lock:
            state = self._state_for(tenant_id)
            sig = candidate_signature_key(candidate)
            if sig in state.failure_records:
                del state.failure_records[sig]
                log.info(
                    "exploration_penalty.failure_cleared_on_success",
                    tenant_id=tenant_id,
                    signature=sig,
                )

    async def filter_candidates(
        self,
        tenant_id: str,
        candidates: list[StrategyExperiment],
    ) -> list[StrategyExperiment]:
        """批量过滤 — blocked 的 candidate 不返回."""
        if not candidates:
            return []
        passed: list[StrategyExperiment] = []
        for c in candidates:
            result = await self.check(tenant_id, c)
            if not result.blocked:
                passed.append(c)
            else:
                log.warning(
                    "exploration_penalty.candidate_blocked",
                    tenant_id=tenant_id,
                    signature=result.signature,
                    failure_count=result.current_failure_count,
                )
        return passed

    async def snapshot(self, tenant_id: str) -> dict[str, Any]:
        async with self._lock:
            now = datetime.now(UTC)
            state = self._state_for(tenant_id)
            state.purge_expired(now, self._window_seconds)
            return {
                "tenant_id": tenant_id,
                "tracked_signatures": list(state.failure_records.keys()),
                "failure_counts": {
                    sig: len(records)
                    for sig, records in state.failure_records.items()
                },
                "max_retries": self._max_retries,
                "window_seconds": self._window_seconds,
            }


__all__ = [
    "DEFAULT_MAX_RETRIES_PER_WINDOW",
    "DEFAULT_PENALTY_WINDOW_SECONDS",
    "ExplorationPenalty",
    "PenaltyCheckResult",
    "PenaltyState",
    "candidate_signature_key",
]
