"""V2.4: AntiGaming 自学新套路 (基于用户 👎 反馈).

用户 POST /api/tasks/{id}/feedback {rating: 1, comment: "LLM 在偷懒, 都是套话"}
→ 这里 collect feedback → LLM 提取新 pattern → 加进 detector dynamic patterns.

简化版: 现在只 collect 反馈到 in-memory list, 给 dashboard 看.
真 LLM 自动 mining V2.4 加 (现在留 placeholder).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class FeedbackPattern:
    """收集到的可能新套路."""

    pattern: str  # 用户写的简短描述, e.g. "LLM 偷懒套话"
    count: int = 1
    examples: list[str] = field(default_factory=list)
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))


class AntiGamingLearner:
    """收集用户 thumbs-down feedback, 暴露给 dashboard / LLM mining."""

    def __init__(self) -> None:
        # tenant -> pattern_key -> FeedbackPattern
        self._patterns: dict[str, dict[str, FeedbackPattern]] = defaultdict(dict)

    def record_negative_feedback(
        self,
        tenant_id: str,
        comment: str,
        task_id: str,
        rating: int,
    ) -> None:
        """rating <= 2 = 负面反馈; comment 提到的关键词当 pattern key."""
        if rating > 2:
            return
        key = (comment[:50] or "low_rating_no_comment").strip().lower()
        existing = self._patterns[tenant_id].get(key)
        if existing is None:
            self._patterns[tenant_id][key] = FeedbackPattern(
                pattern=key,
                examples=[task_id],
            )
        else:
            existing.count += 1
            existing.last_seen = datetime.now(UTC)
            if task_id not in existing.examples:
                existing.examples.append(task_id)

    def top_patterns(self, tenant_id: str, *, limit: int = 5) -> list[FeedbackPattern]:
        """返高频反馈 pattern. dashboard 可显示."""
        items = list(self._patterns.get(tenant_id, {}).values())
        items.sort(key=lambda p: (-p.count, -p.last_seen.timestamp()))
        return items[:limit]

    def reset(self) -> None:
        self._patterns.clear()


_singleton: AntiGamingLearner | None = None


def get_anti_gaming_learner() -> AntiGamingLearner:
    global _singleton
    if _singleton is None:
        _singleton = AntiGamingLearner()
    return _singleton


def reset_anti_gaming_learner() -> None:
    global _singleton
    _singleton = None


__all__ = [
    "AntiGamingLearner",
    "FeedbackPattern",
    "get_anti_gaming_learner",
    "reset_anti_gaming_learner",
]
