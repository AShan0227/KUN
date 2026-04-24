"""Subscription quota tracker — 5h rolling window + downgrade chain.

ADR-002 amendment (2026-04-24): Claude Code CLI (OAuth) is the default for
top/strong/cheap tiers. Because Claude Pro/Max subscriptions have an
undocumented-but-real 5-hour rate limit, we track per-tier usage here and
downgrade when a tier approaches its ceiling:

    top (opus) → strong (sonnet) → cheap (haiku) → fallback (MiniMax)

Limits are conservative defaults that can be overridden via env vars
(``KUN_QUOTA_TOP`` / ``KUN_QUOTA_STRONG`` / ``KUN_QUOTA_CHEAP``) or by
passing ``limits=`` to :class:`QuotaTracker`.

Usage-driven, not time-of-day. Process-local state — restart resets the
window. For multi-process deployment wire a Redis-backed impl later.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Literal

TierName = Literal["top", "strong", "cheap", "fallback"]

# Downgrade chain: if your target tier is saturated, walk this list.
DOWNGRADE: dict[TierName, TierName | None] = {
    "top": "strong",
    "strong": "cheap",
    "cheap": "fallback",
    "fallback": None,  # nowhere left to go — surface the exhaustion
}

# Conservative 5h call-count ceilings for a Claude Pro subscription.
# Real limits are token-based and opaque; count-based is the cheapest safe
# proxy. Tune via env if you see premature downgrades.
_DEFAULT_LIMITS: dict[TierName, int] = {
    "top": 40,  # Opus 4.7 — expensive tokens, tight budget
    "strong": 200,  # Sonnet 4.6 — middle ground
    "cheap": 1000,  # Haiku 4.5 — essentially free
    "fallback": 10_000,  # MiniMax — paid per call, separate budget
}

_WINDOW_SEC = 5 * 3600


def _load_limits_from_env() -> dict[TierName, int]:
    return {
        "top": int(os.getenv("KUN_QUOTA_TOP", _DEFAULT_LIMITS["top"])),
        "strong": int(os.getenv("KUN_QUOTA_STRONG", _DEFAULT_LIMITS["strong"])),
        "cheap": int(os.getenv("KUN_QUOTA_CHEAP", _DEFAULT_LIMITS["cheap"])),
        "fallback": int(os.getenv("KUN_QUOTA_FALLBACK", _DEFAULT_LIMITS["fallback"])),
    }


class QuotaTracker:
    """5h rolling per-tier call counter with downgrade suggestion."""

    def __init__(
        self,
        limits: dict[TierName, int] | None = None,
        window_sec: int = _WINDOW_SEC,
    ) -> None:
        self._limits = limits or _load_limits_from_env()
        self._window = window_sec
        self._buckets: dict[TierName, deque[float]] = {t: deque() for t in self._limits}
        self._lock = threading.Lock()

    # ---------- observation ----------

    def record(self, tier: TierName) -> None:
        """Log one call against the tier's rolling window."""
        if tier not in self._buckets:
            return
        with self._lock:
            self._prune_locked()
            self._buckets[tier].append(time.monotonic())

    def usage(self, tier: TierName) -> int:
        """How many calls against ``tier`` in the current 5h window."""
        with self._lock:
            self._prune_locked()
            return len(self._buckets.get(tier, ()))

    def saturated(self, tier: TierName) -> bool:
        """True if ``tier`` has hit its configured ceiling."""
        return self.usage(tier) >= self._limits.get(tier, 1 << 30)

    def headroom(self, tier: TierName) -> int:
        """Remaining calls in the window (>=0)."""
        return max(0, self._limits.get(tier, 0) - self.usage(tier))

    # ---------- decision helper ----------

    def resolve(self, tier: TierName) -> TierName:
        """Walk the downgrade chain until a non-saturated tier is found.

        Returns the original tier if it has headroom; otherwise the first
        healthier downstream tier. Falls through to "fallback" when all
        top/strong/cheap are saturated.
        """
        cursor: TierName | None = tier
        while cursor is not None:
            if not self.saturated(cursor):
                return cursor
            cursor = DOWNGRADE.get(cursor)
        # Everything saturated — return the tail (fallback). Caller's retry
        # loop / circuit breaker is responsible for what to do next.
        return "fallback"

    # ---------- lifecycle ----------

    def reset(self) -> None:
        with self._lock:
            for q in self._buckets.values():
                q.clear()

    def snapshot(self) -> dict[str, int]:
        """For /metrics or debug — current usage per tier."""
        return {t: self.usage(t) for t in self._buckets}

    # ---------- internals ----------

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - self._window
        for q in self._buckets.values():
            while q and q[0] < cutoff:
                q.popleft()


# ------------- module-level singleton (router uses this) -------------

_tracker: QuotaTracker | None = None


def get_tracker() -> QuotaTracker:
    global _tracker
    if _tracker is None:
        _tracker = QuotaTracker()
    return _tracker


def set_tracker(tracker: QuotaTracker) -> None:
    """For tests — inject a fresh tracker."""
    global _tracker
    _tracker = tracker


def reset_tracker() -> None:
    global _tracker
    _tracker = None
