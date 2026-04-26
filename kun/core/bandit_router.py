"""Bandit router primitives for adaptive model/tool selection.

C13 is intentionally standalone. Claude can wire this into the live LLM router
later without changing the public contract here.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class ArmStats:
    """Running reward stats for one arm in one context."""

    count: int = 0
    total_reward: float = 0.0

    @property
    def average_reward(self) -> float:
        if self.count == 0:
            return 0.0
        return self.total_reward / self.count

    def update(self, reward: float) -> None:
        self.count += 1
        self.total_reward += reward


class EpsilonGreedyBandit:
    """Small epsilon-greedy learner keyed by task/context type."""

    def __init__(
        self,
        arms: list[str],
        epsilon: float = 0.1,
        *,
        rng: random.Random | None = None,
    ) -> None:
        if not arms:
            raise ValueError("arms must not be empty")
        if len(set(arms)) != len(arms):
            raise ValueError("arms must be unique")
        self.arms = list(arms)
        self.epsilon = max(0.0, min(1.0, epsilon))
        self._rng = rng or random.Random()
        self._stats: dict[str, dict[str, ArmStats]] = {}

    def select(self, context_key: str) -> str:
        """Pick an arm.

        epsilon chance explores randomly; otherwise exploits the current best arm.
        """

        self._ensure_context(context_key)
        if self.epsilon > 0.0 and self._rng.random() < self.epsilon:
            return self._rng.choice(self.arms)
        return self.best_arm(context_key)

    def update(self, context_key: str, arm: str, reward: float) -> None:
        """Update average reward for a (context, arm) pair."""

        self._ensure_arm(arm)
        self._ensure_context(context_key)
        self._stats[context_key][arm].update(_clamp_reward(reward))

    def best_arm(self, context_key: str) -> str:
        """Return the highest average-reward arm for this context."""

        self._ensure_context(context_key)
        stats = self._stats[context_key]
        return max(
            self.arms,
            key=lambda arm: (stats[arm].average_reward, stats[arm].count, -self.arms.index(arm)),
        )

    def stats_for(self, context_key: str) -> dict[str, ArmStats]:
        """Return a copy of stats for monitoring/tests."""

        self._ensure_context(context_key)
        return {
            arm: ArmStats(count=stats.count, total_reward=stats.total_reward)
            for arm, stats in self._stats[context_key].items()
        }

    def _ensure_context(self, context_key: str) -> None:
        if not context_key:
            raise ValueError("context_key must not be empty")
        if context_key not in self._stats:
            self._stats[context_key] = {arm: ArmStats() for arm in self.arms}

    def _ensure_arm(self, arm: str) -> None:
        if arm not in self.arms:
            raise ValueError(f"unknown arm: {arm}")


@dataclass(frozen=True)
class RollbackRecord:
    arm: str
    success: bool
    recorded_at: datetime


class AutoRollback:
    """Track recent failures and roll back to the last known good arm."""

    def __init__(
        self,
        failure_threshold: int = 3,
        window_sec: int = 600,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self.failure_threshold = failure_threshold
        self.window_sec = window_sec
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events: deque[RollbackRecord] = deque()
        self._last_known_good: str | None = None

    def record(self, arm: str, success: bool) -> None:
        """Record one outcome and update last known good on success."""

        if not arm:
            raise ValueError("arm must not be empty")
        now = self._clock()
        self._events.append(RollbackRecord(arm=arm, success=success, recorded_at=now))
        self._prune(now)
        if success:
            self._last_known_good = arm

    def should_rollback(self, current_arm: str) -> tuple[bool, str | None]:
        """Return whether current_arm should roll back and to which arm."""

        if not current_arm:
            raise ValueError("current_arm must not be empty")
        now = self._clock()
        self._prune(now)
        if self._last_known_good is None or self._last_known_good == current_arm:
            return (False, None)
        failures = self._consecutive_failures(current_arm)
        if failures >= self.failure_threshold:
            return (True, self._last_known_good)
        return (False, None)

    @property
    def last_known_good(self) -> str | None:
        return self._last_known_good

    def recent_records(self) -> list[RollbackRecord]:
        self._prune(self._clock())
        return list(self._events)

    def _consecutive_failures(self, arm: str) -> int:
        failures = 0
        for event in reversed(self._events):
            if event.arm != arm:
                continue
            if event.success:
                break
            failures += 1
        return failures

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_sec)
        while self._events and self._events[0].recorded_at < cutoff:
            self._events.popleft()


def _clamp_reward(reward: float) -> float:
    return max(0.0, min(1.0, float(reward)))


__all__ = [
    "ArmStats",
    "AutoRollback",
    "EpsilonGreedyBandit",
    "RollbackRecord",
]
