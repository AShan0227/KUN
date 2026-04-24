"""Quota tracker: rolling window, saturation, downgrade chain."""

from __future__ import annotations

import pytest
from kun.core.quota_tracker import DOWNGRADE, QuotaTracker


@pytest.mark.unit
def test_record_increments_usage():
    t = QuotaTracker(limits={"top": 2, "strong": 10, "cheap": 100, "fallback": 1000})
    assert t.usage("top") == 0
    t.record("top")
    assert t.usage("top") == 1
    t.record("top")
    assert t.usage("top") == 2


@pytest.mark.unit
def test_saturation_detected_at_limit():
    t = QuotaTracker(limits={"top": 2, "strong": 10, "cheap": 100, "fallback": 1000})
    t.record("top")
    assert not t.saturated("top")
    t.record("top")
    assert t.saturated("top")


@pytest.mark.unit
def test_headroom_is_nonnegative():
    t = QuotaTracker(limits={"top": 2, "strong": 10, "cheap": 100, "fallback": 1000})
    assert t.headroom("top") == 2
    t.record("top")
    t.record("top")
    t.record("top")  # over
    assert t.headroom("top") == 0


@pytest.mark.unit
def test_resolve_walks_downgrade_chain():
    t = QuotaTracker(limits={"top": 0, "strong": 0, "cheap": 100, "fallback": 1000})
    # top saturated (limit 0) → strong saturated → cheap has room
    assert t.resolve("top") == "cheap"


@pytest.mark.unit
def test_resolve_returns_original_when_healthy():
    t = QuotaTracker(limits={"top": 10, "strong": 10, "cheap": 100, "fallback": 1000})
    assert t.resolve("top") == "top"
    assert t.resolve("cheap") == "cheap"


@pytest.mark.unit
def test_resolve_falls_through_to_fallback_when_all_saturated():
    t = QuotaTracker(
        limits={"top": 0, "strong": 0, "cheap": 0, "fallback": 1000},
    )
    # Everything upstream saturated → fallback
    assert t.resolve("top") == "fallback"


@pytest.mark.unit
def test_downgrade_chain_shape():
    # Safety net: make sure the hardcoded chain still goes top→strong→cheap→fallback→None.
    assert DOWNGRADE["top"] == "strong"
    assert DOWNGRADE["strong"] == "cheap"
    assert DOWNGRADE["cheap"] == "fallback"
    assert DOWNGRADE["fallback"] is None


@pytest.mark.unit
def test_window_prunes_old_entries():
    # Tiny window so we can exercise it without sleeping
    t = QuotaTracker(
        limits={"top": 10, "strong": 10, "cheap": 100, "fallback": 1000},
        window_sec=0,  # everything is "old" immediately
    )
    t.record("top")
    # Reading usage forces a prune; with window_sec=0 the entry is already stale.
    assert t.usage("top") == 0


@pytest.mark.unit
def test_reset_clears_all_buckets():
    t = QuotaTracker(limits={"top": 10, "strong": 10, "cheap": 100, "fallback": 1000})
    t.record("top")
    t.record("strong")
    t.reset()
    assert t.snapshot() == {"top": 0, "strong": 0, "cheap": 0, "fallback": 0}
