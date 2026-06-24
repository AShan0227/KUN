"""L5.3 — Priority Channel 单测."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kun.governance.priority_channel import (
    classify_request_priority,
    is_high_priority_channel,
    prioritize_requests,
    split_by_tier,
)


def _req(
    *,
    triggered_by: str = "anomaly_threshold",
    anomaly_kind: str = "task_failure_spike",
    priority: str = "medium",
    created_at: datetime | None = None,
    request_id: str = "ss-1",
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "triggered_by": triggered_by,
        "anomaly_kind": anomaly_kind,
        "priority": priority,
        "created_at": created_at or datetime.now(UTC),
    }


# ---- classify_request_priority ----


def test_anomaly_cluster_is_urgent() -> None:
    cls = classify_request_priority(_req(triggered_by="anomaly_cluster"))
    assert cls.tier == "urgent"
    assert cls.boosted is True


def test_promotion_expired_is_urgent() -> None:
    cls = classify_request_priority(
        _req(triggered_by="promotion_timeout", anomaly_kind="promotion_expired")
    )
    assert cls.tier == "urgent"
    assert cls.boosted is True


def test_promotion_stale_is_high() -> None:
    cls = classify_request_priority(
        _req(triggered_by="promotion_timeout", anomaly_kind="promotion_stale")
    )
    assert cls.tier == "high"
    assert cls.boosted is True


def test_anomaly_threshold_keeps_original_priority_high() -> None:
    cls = classify_request_priority(_req(priority="high"))
    assert cls.tier == "high"
    assert cls.boosted is False


def test_anomaly_threshold_keeps_original_priority_medium() -> None:
    cls = classify_request_priority(_req(priority="medium"))
    assert cls.tier == "medium"
    assert cls.boosted is False


def test_anomaly_threshold_keeps_original_priority_low() -> None:
    cls = classify_request_priority(_req(priority="low"))
    assert cls.tier == "low"
    assert cls.boosted is False


def test_missing_priority_defaults_medium() -> None:
    req = {"triggered_by": "anomaly_threshold"}
    cls = classify_request_priority(req)
    assert cls.tier == "medium"


# ---- is_high_priority_channel ----


def test_is_high_priority_channel_for_urgent() -> None:
    assert is_high_priority_channel(_req(triggered_by="anomaly_cluster")) is True


def test_is_high_priority_channel_for_high() -> None:
    assert is_high_priority_channel(_req(priority="high")) is True


def test_is_high_priority_channel_false_for_medium() -> None:
    assert is_high_priority_channel(_req(priority="medium")) is False


def test_is_high_priority_channel_false_for_low() -> None:
    assert is_high_priority_channel(_req(priority="low")) is False


# ---- prioritize_requests ----


def test_prioritize_empty() -> None:
    assert prioritize_requests([]) == []


def test_prioritize_urgent_before_high() -> None:
    now = datetime.now(UTC)
    urgent = _req(
        request_id="r-urgent",
        triggered_by="anomaly_cluster",
        created_at=now,
    )
    high = _req(
        request_id="r-high",
        priority="high",
        created_at=now - timedelta(minutes=5),  # 比 urgent 早 5 分钟
    )
    sorted_pairs = prioritize_requests([high, urgent])
    # urgent 排前面 (尽管 high 先到)
    assert sorted_pairs[0][0]["request_id"] == "r-urgent"
    assert sorted_pairs[1][0]["request_id"] == "r-high"


def test_prioritize_fifo_within_same_tier() -> None:
    """同 tier 按 created_at 升序 (FIFO)."""
    base = datetime.now(UTC)
    a = _req(request_id="r-a", priority="medium", created_at=base)
    b = _req(request_id="r-b", priority="medium", created_at=base + timedelta(minutes=1))
    c = _req(request_id="r-c", priority="medium", created_at=base + timedelta(minutes=2))
    sorted_pairs = prioritize_requests([c, a, b])
    ids = [p[0]["request_id"] for p in sorted_pairs]
    assert ids == ["r-a", "r-b", "r-c"]


def test_prioritize_full_order() -> None:
    now = datetime.now(UTC)
    urgent = _req(request_id="u", triggered_by="anomaly_cluster", created_at=now)
    high = _req(request_id="h", priority="high", created_at=now)
    medium = _req(request_id="m", priority="medium", created_at=now)
    low = _req(request_id="l", priority="low", created_at=now)
    sorted_pairs = prioritize_requests([low, medium, high, urgent])
    ids = [p[0]["request_id"] for p in sorted_pairs]
    assert ids == ["u", "h", "m", "l"]


def test_prioritize_handles_missing_created_at() -> None:
    """缺 created_at 字段不抛 — 当 0 timestamp."""
    a = {"request_id": "r-a", "triggered_by": "anomaly_threshold", "priority": "medium"}
    b = _req(request_id="r-b", priority="medium")
    sorted_pairs = prioritize_requests([b, a])
    # 不抛即可, 顺序由 timestamp=0 与 b 真实 timestamp 决定
    assert len(sorted_pairs) == 2


# ---- split_by_tier ----


def test_split_by_tier_buckets() -> None:
    requests = [
        _req(triggered_by="anomaly_cluster"),  # urgent
        _req(triggered_by="promotion_timeout", anomaly_kind="promotion_expired"),  # urgent
        _req(priority="high"),  # high
        _req(priority="medium"),  # medium
        _req(priority="low"),  # low
    ]
    buckets = split_by_tier(requests)
    assert len(buckets["urgent"]) == 2
    assert len(buckets["high"]) == 1
    assert len(buckets["medium"]) == 1
    assert len(buckets["low"]) == 1


def test_split_by_tier_empty_buckets_present() -> None:
    """没有 request 时, 所有 tier 桶仍存在 (空 list)."""
    buckets = split_by_tier([])
    assert set(buckets.keys()) == {"urgent", "high", "medium", "low"}
    for v in buckets.values():
        assert v == []
