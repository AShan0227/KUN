"""LLM provider 故障转移保护。

这个模块只维护 provider 级别的健康状态：连续失败达到阈值后进入冷却，
冷却期间 router 会跳过它，优先尝试备选 provider。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from kun.interface.llm.base import LLMProvider, ModelTier

ProviderKey = tuple[ModelTier, str]


@dataclass(frozen=True)
class FailoverPolicy:
    """故障转移策略。"""

    primary: str
    backup: list[str]
    failure_threshold: int = 3
    cooldown_sec: int = 300


@dataclass
class _ProviderState:
    tier: ModelTier
    provider_name: str
    consecutive_failures: int = 0
    cooling_until: float = 0.0


@dataclass
class FailoverGuard:
    """记录 provider 失败，并给 router 一个可跳过名单。

    状态 key 是 ``(tier, provider_name)``，不是单独的 provider_name。
    原因很现实：同一个 provider 名字可能同时服务 top/strong/cheap 三档。
    如果只按名字记账，cheap 成功会误清 top 的失败计数。
    """

    policy: FailoverPolicy
    tier_provider_names: dict[ModelTier, str] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    _states: dict[ProviderKey, _ProviderState] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        for tier, provider_name in self.tier_provider_names.items():
            self._states.setdefault((tier, provider_name), _ProviderState(tier, provider_name))

    @classmethod
    def from_providers(cls, providers: dict[ModelTier, LLMProvider]) -> FailoverGuard | None:
        """从 router providers 自动生成默认故障转移策略。"""
        if not providers:
            return None
        primary_provider = providers.get("top") or next(iter(providers.values()))
        backup_names: list[str] = []
        for tier in ("strong", "cheap", "coding", "fallback"):
            provider = providers.get(tier)
            if provider is None or provider.name == primary_provider.name:
                continue
            if provider.name not in backup_names:
                backup_names.append(provider.name)
        if not backup_names:
            return None
        return cls(
            FailoverPolicy(primary=primary_provider.name, backup=backup_names),
            tier_provider_names={tier: provider.name for tier, provider in providers.items()},
        )

    async def record_failure(self, provider_name: str, *, tier: ModelTier) -> bool:
        """记录一次失败；返回这次是否触发切换。"""
        async with self._lock:
            state = self._state(tier, provider_name)
            state.consecutive_failures += 1
            if state.consecutive_failures < self.policy.failure_threshold:
                return False

            state.cooling_until = self.clock() + self.policy.cooldown_sec
            return True

    async def record_success(self, provider_name: str, *, tier: ModelTier) -> None:
        """成功后清掉该 provider 的连续失败计数。"""
        async with self._lock:
            state = self._state(tier, provider_name)
            state.consecutive_failures = 0
            state.cooling_until = 0.0

    async def current_active(self, *, primary_tier: ModelTier = "top") -> str:
        """当前应该优先使用的 provider。"""
        async with self._lock:
            provider_name = self.tier_provider_names.get(primary_tier, self.policy.primary)
            if self._is_available_unlocked(primary_tier, provider_name):
                return provider_name
            for tier in self.candidate_order(primary_tier, "fallback"):
                candidate = self.tier_provider_names.get(tier)
                if candidate and self._is_available_unlocked(tier, candidate):
                    return candidate
            return provider_name

    async def is_available(self, provider_name: str, *, tier: ModelTier) -> bool:
        """冷却期内不可用；冷却结束后自动恢复。"""
        async with self._lock:
            return self._is_available_unlocked(tier, provider_name)

    def candidate_order(self, primary_tier: ModelTier, fallback_tier: ModelTier) -> list[ModelTier]:
        """给 router 的候选 tier 顺序。

        A/B 负责选某个 tier 里的 provider 实例；failover 只决定这个 tier
        当前要不要试，以及下一个 tier 是谁。
        """
        order: list[ModelTier] = [primary_tier]
        preferred_names = [self.policy.primary, *self.policy.backup]
        for provider_name in preferred_names:
            for tier, candidate_name in self.tier_provider_names.items():
                if candidate_name == provider_name and tier not in order:
                    order.append(tier)
        if fallback_tier not in order:
            order.append(fallback_tier)
        for tier in self.tier_provider_names:
            if tier not in order:
                order.append(tier)
        return order

    def _state(self, tier: ModelTier, provider_name: str) -> _ProviderState:
        return self._states.setdefault(
            (tier, provider_name),
            _ProviderState(tier=tier, provider_name=provider_name),
        )

    def _is_available_unlocked(self, tier: ModelTier, provider_name: str) -> bool:
        state = self._state(tier, provider_name)
        if state.cooling_until <= 0:
            return True
        if self.clock() >= state.cooling_until:
            state.cooling_until = 0.0
            state.consecutive_failures = 0
            return True
        return False


__all__ = ["FailoverGuard", "FailoverPolicy"]
