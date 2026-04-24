"""Subscription quota tracker — 5h rolling window + soft warnings.

ADR-002 amendment (2026-04-24, test-phase policy):

    User is in product-testing mode, not cost-sensitive. We do NOT auto-downgrade
    Opus / Sonnet / Haiku — the user wants the best tier for each request and
    is paying a flat subscription anyway. What we DO want is a *heads-up*
    before the Claude Pro 5h rate limit bites.

So the tracker now has two concepts:

  - **limit (hard)**   — if exceeded, :meth:`resolve` walks the downgrade chain.
                          Default `None` for top/strong/cheap (never downgrade).
                          Kept as `10_000` for `fallback` (MiniMax is paid).
  - **warn_at (soft)** — on cross, logs `quota.approaching_limit` once per
                          30-min window. Lets the user know "hey, Opus has
                          been used ~N times in 5h — heading toward the wall".

Env overrides:
  - ``KUN_QUOTA_TOP`` / ``..._STRONG`` / ``..._CHEAP`` / ``..._FALLBACK``:
     integer hard limit (or ``none`` to disable).
  - ``KUN_QUOTA_WARN_TOP`` / ``..._WARN_STRONG`` / ``..._WARN_CHEAP``:
     integer soft-warn threshold.

Process-local state — restart resets the window. For multi-process deployment
wire a Redis-backed impl later.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Literal

from kun.core.logging import get_logger

log = get_logger("kun.quota")

TierName = Literal["top", "strong", "cheap", "fallback"]

# Downgrade chain: if your target tier is saturated, walk this list.
DOWNGRADE: dict[TierName, TierName | None] = {
    "top": "strong",
    "strong": "cheap",
    "cheap": "fallback",
    "fallback": None,  # nowhere left to go — surface the exhaustion
}

# Hard ceilings. ``None`` disables downgrade for the tier.
# User is on a Pro subscription and explicitly opted OUT of auto-downgrade
# during the test phase — we only cap fallback (MiniMax) which is metered.
_DEFAULT_LIMITS: dict[TierName, int | None] = {
    "top": None,  # Opus — never auto-downgrade
    "strong": None,  # Sonnet — never auto-downgrade
    "cheap": None,  # Haiku — never auto-downgrade
    "fallback": 10_000,  # MiniMax — paid per call, keep a soft cap
}

# Soft warn thresholds (5h rolling window). 0 / None = don't warn.
# These are rough estimates of where Claude Pro's 5h rate limit starts to bite.
# Tune by observation.
_DEFAULT_WARNS: dict[TierName, int] = {
    "top": 30,  # Opus: ~30 calls / 5h → start worrying about rate limit
    "strong": 150,
    "cheap": 0,  # Haiku: don't bother warning
    "fallback": 0,
}

_WINDOW_SEC = 5 * 3600

# Don't spam the same warning — re-log at most once every 30 min per tier.
_WARN_REPEAT_SEC = 30 * 60


def _int_or_none(raw: str | None, default: int | None) -> int | None:
    if raw is None:
        return default
    low = raw.strip().lower()
    if low in {"", "none", "null", "off", "-1", "inf"}:
        return None
    try:
        return int(low)
    except ValueError:
        return default


def _load_limits_from_env() -> dict[TierName, int | None]:
    return {
        "top": _int_or_none(os.getenv("KUN_QUOTA_TOP"), _DEFAULT_LIMITS["top"]),
        "strong": _int_or_none(os.getenv("KUN_QUOTA_STRONG"), _DEFAULT_LIMITS["strong"]),
        "cheap": _int_or_none(os.getenv("KUN_QUOTA_CHEAP"), _DEFAULT_LIMITS["cheap"]),
        "fallback": _int_or_none(os.getenv("KUN_QUOTA_FALLBACK"), _DEFAULT_LIMITS["fallback"]),
    }


def _load_warns_from_env() -> dict[TierName, int]:
    return {
        "top": int(os.getenv("KUN_QUOTA_WARN_TOP", _DEFAULT_WARNS["top"])),
        "strong": int(os.getenv("KUN_QUOTA_WARN_STRONG", _DEFAULT_WARNS["strong"])),
        "cheap": int(os.getenv("KUN_QUOTA_WARN_CHEAP", _DEFAULT_WARNS["cheap"])),
        "fallback": int(os.getenv("KUN_QUOTA_WARN_FALLBACK", _DEFAULT_WARNS["fallback"])),
    }


class QuotaTracker:
    """5h rolling per-tier call counter — soft warn + optional hard cap."""

    def __init__(
        self,
        limits: dict[TierName, int | None] | None = None,
        warns: dict[TierName, int] | None = None,
        window_sec: int = _WINDOW_SEC,
    ) -> None:
        self._limits: dict[TierName, int | None] = (
            limits if limits is not None else _load_limits_from_env()
        )
        self._warns: dict[TierName, int] = warns if warns is not None else _load_warns_from_env()
        self._window = window_sec
        self._buckets: dict[TierName, deque[float]] = {t: deque() for t in self._limits}
        # last time we emitted a warning for each tier — throttles repeats
        self._last_warn_at: dict[TierName, float] = dict.fromkeys(self._limits, 0.0)
        self._lock = threading.Lock()

    # ---------- observation ----------

    def record(self, tier: TierName) -> None:
        """Log one call against the tier's rolling window. May emit a warning."""
        if tier not in self._buckets:
            return
        with self._lock:
            self._prune_locked()
            self._buckets[tier].append(time.monotonic())
            current = len(self._buckets[tier])
            warn_at = self._warns.get(tier, 0)
            last_warn = self._last_warn_at.get(tier, 0.0)
        # Warn outside the lock so the log call can't deadlock
        if (
            warn_at > 0
            and current >= warn_at
            and (time.monotonic() - last_warn) >= _WARN_REPEAT_SEC
        ):
            limit = self._limits.get(tier)
            log.warning(
                "quota.approaching_limit",
                tier=tier,
                usage_5h=current,
                warn_at=warn_at,
                hard_limit=limit if limit is not None else "unlimited",
                hint=(
                    f"heads-up: {tier} tier at {current}/5h (warn_at={warn_at}). "
                    "if you hit the Claude Pro 5h rate limit, set "
                    f"KUN_QUOTA_{tier.upper()}=<N> to force a downgrade before that."
                ),
            )
            with self._lock:
                self._last_warn_at[tier] = time.monotonic()

    def usage(self, tier: TierName) -> int:
        """How many calls against ``tier`` in the current 5h window."""
        with self._lock:
            self._prune_locked()
            return len(self._buckets.get(tier, ()))

    def saturated(self, tier: TierName) -> bool:
        """True if ``tier`` has hit its configured hard ceiling.

        ``None`` limit = never saturated (the user opted out of downgrade).
        """
        limit = self._limits.get(tier)
        if limit is None:
            return False
        return self.usage(tier) >= limit

    def headroom(self, tier: TierName) -> int | None:
        """Remaining calls in the window. ``None`` = unlimited."""
        limit = self._limits.get(tier)
        if limit is None:
            return None
        return max(0, limit - self.usage(tier))

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
