"""Priority Channel — Supervisor cluster / promotion_timeout → Strategist 高优 (L5.3).

L5 §交付标志: "监督线发现的系统性问题 → 自动转 Strategist 任务".
正常 strategy_search_request 走默认 priority (medium/high), 但系统性信号
(L5.1 cluster / L5.2 promotion_timeout) 应该被 Strategist 优先消费.

工程化实现 = static priority classifier + `prioritize_requests` pure 函数:

  PRIORITY TIER:
    urgent  → anomaly_cluster + promotion_expired
    high    → promotion_stale + 原 priority=high
    medium  → 原 priority=medium
    low     → 原 priority=low

  consumer 按 urgent → high → medium → low + FIFO 排序消费.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PriorityTier = Literal["urgent", "high", "medium", "low"]


_PRIORITY_ORDER: dict[PriorityTier, int] = {
    "urgent": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


@dataclass(frozen=True)
class PriorityClassification:
    """单 request 的优先级评定结果."""

    tier: PriorityTier
    reason: str
    boosted: bool  # True 表示 systemic 信号被提升


def classify_request_priority(request: dict[str, Any]) -> PriorityClassification:
    """根据 triggered_by + anomaly_kind 决定 priority tier.

    规则:
      anomaly_cluster                → urgent (系统性问题, 多 anomaly 聚合)
      promotion_timeout + expired     → urgent (capability 卡死, 影响 RSI 闭环)
      promotion_timeout + stale       → high   (卡但未死)
      anomaly_threshold + priority=high   → high
      anomaly_threshold + priority=medium → medium
      anomaly_threshold + priority=low    → low
      其他                            → medium (默认)
    """
    triggered_by = request.get("triggered_by") or ""
    anomaly_kind = request.get("anomaly_kind") or ""
    base_priority = (request.get("priority") or "medium").lower()

    if triggered_by == "anomaly_cluster":
        return PriorityClassification(
            tier="urgent",
            reason=f"systemic anomaly_cluster ({anomaly_kind})",
            boosted=True,
        )

    if triggered_by == "promotion_timeout":
        if anomaly_kind == "promotion_expired":
            return PriorityClassification(
                tier="urgent",
                reason="capability expired (promotion deadline exceeded)",
                boosted=True,
            )
        # promotion_stale or 其他
        return PriorityClassification(
            tier="high",
            reason=f"promotion stale ({anomaly_kind})",
            boosted=True,
        )

    # 普通 anomaly_threshold — 跟随原 priority 字段
    if base_priority == "high":
        return PriorityClassification(
            tier="high",
            reason=f"base priority=high ({triggered_by or 'unspecified'})",
            boosted=False,
        )
    if base_priority == "low":
        return PriorityClassification(
            tier="low",
            reason=f"base priority=low ({triggered_by or 'unspecified'})",
            boosted=False,
        )
    return PriorityClassification(
        tier="medium",
        reason=f"base priority=medium ({triggered_by or 'unspecified'})",
        boosted=False,
    )


def prioritize_requests(
    requests: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], PriorityClassification]]:
    """对 requests 列表按优先级 + FIFO 排序.

    Returns: [(request, classification), ...] — urgent 在前; 同 tier 内
    按 created_at 升序 (FIFO).
    """
    if not requests:
        return []

    classified: list[tuple[dict[str, Any], PriorityClassification]] = [
        (req, classify_request_priority(req)) for req in requests
    ]

    def sort_key(item: tuple[dict[str, Any], PriorityClassification]):
        req, cls = item
        # 同 tier 内按 created_at 升序 (FIFO)
        created = req.get("created_at")
        if created is None:
            timestamp = 0.0
        elif hasattr(created, "timestamp"):
            timestamp = created.timestamp()
        else:
            timestamp = 0.0
        return (_PRIORITY_ORDER[cls.tier], timestamp)

    return sorted(classified, key=sort_key)


def split_by_tier(
    requests: list[dict[str, Any]],
) -> dict[PriorityTier, list[dict[str, Any]]]:
    """把 requests 按 tier 分桶 — 给 Strategist 按桶批处理."""
    buckets: dict[PriorityTier, list[dict[str, Any]]] = {
        "urgent": [],
        "high": [],
        "medium": [],
        "low": [],
    }
    for req in requests:
        cls = classify_request_priority(req)
        buckets[cls.tier].append(req)
    return buckets


def is_high_priority_channel(request: dict[str, Any]) -> bool:
    """便于 Supervisor / Strategist 快速判断: 这条 request 是否走高优通道."""
    cls = classify_request_priority(request)
    return cls.tier in ("urgent", "high")


__all__ = [
    "PriorityClassification",
    "PriorityTier",
    "classify_request_priority",
    "is_high_priority_channel",
    "prioritize_requests",
    "split_by_tier",
]
