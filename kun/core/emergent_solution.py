"""EmergentSolution — 涌现方案候选 (V2.1 §5.8 / §13.9).

任务执行中识别"现在的方案不够好" → 候选新方案. 走 §8.3 渐进部署:
candidate → shadow_testing → canary → stable / rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id

DiscoveredBy = Literal[
    "external_scan",  # §3.10 异步守望外部检索
    "llm_metacognitive",  # §17.10 模式 C
    "capability_card_query",  # §13.2 历史更优路径
    "user_correction",  # WS correction
    "watchtower_signal",  # 守望主动发现
    "surprise_history",  # surprise_score 持续偏高触发
    "learning_emergent",  # idle-batch 涌现学习
]

SourceKind = Literal[
    "github_issue",
    "arxiv",
    "reddit",
    "hackernews",
    "internal_history",
    "llm_judgment",
    "competitor_changelog",
]

SolutionStatus = Literal[
    "candidate",
    "shadow_testing",
    "canary",
    "stable",
    "rejected",
]


class EmergentSource(BaseModel):
    kind: SourceKind
    url: str = ""
    snippet: str = ""


class EmergentSolution(BaseModel):
    """涌现方案."""

    solution_id: str = Field(default_factory=lambda: new_id("es"))
    task_type: str
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    discovered_by: DiscoveredBy
    source: EmergentSource

    description: str = ""
    estimated_outcome_delta: float = 0.0  # 预估效果提升 (对比当前)
    estimated_cost_delta: float = 0.0  # 预估成本变化 (负为下降)
    estimated_latency_delta: float = 0.0  # 预估延迟变化

    applies_when: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)

    status: SolutionStatus = "candidate"
    shadow_test_runs: int = 0
    shadow_test_outcome: float = 0.0
    promoted_to_canary_at: datetime | None = None
    canary_traffic_percent: int = 0
    promoted_to_stable_at: datetime | None = None
    rejection_reason: str | None = None

    applied_count: int = 0
    applied_outcome_history: list[float] = Field(default_factory=list)
    user_feedback_score: float | None = None


class EmergentSolutionLibrary:
    """涌现方案候选库 (内存 + 可持久化扩展点)."""

    def __init__(self) -> None:
        self._solutions: dict[str, EmergentSolution] = {}

    def add(self, solution: EmergentSolution) -> None:
        self._solutions[solution.solution_id] = solution

    def get(self, solution_id: str) -> EmergentSolution | None:
        return self._solutions.get(solution_id)

    def list_for_task_type(
        self,
        task_type: str,
        statuses: tuple[SolutionStatus, ...] | None = None,
    ) -> list[EmergentSolution]:
        """取该 task_type 的所有候选."""
        out = []
        for s in self._solutions.values():
            # 支持层级匹配 (coding.python.fastapi 命中 coding / coding.python)
            if not (s.task_type == task_type or task_type.startswith(s.task_type + ".")):
                continue
            if statuses and s.status not in statuses:
                continue
            out.append(s)
        return out

    def has_active_for(self, task_type: str) -> bool:
        """该 task_type 是否有 shadow / canary / stable 候选."""
        return bool(
            self.list_for_task_type(task_type, statuses=("shadow_testing", "canary", "stable"))
        )

    def promote(self, solution_id: str, target: SolutionStatus) -> bool:
        s = self._solutions.get(solution_id)
        if not s:
            return False
        s.status = target
        if target == "canary":
            s.promoted_to_canary_at = datetime.now(UTC)
        elif target == "stable":
            s.promoted_to_stable_at = datetime.now(UTC)
        return True

    def reject(self, solution_id: str, reason: str) -> bool:
        s = self._solutions.get(solution_id)
        if not s:
            return False
        s.status = "rejected"
        s.rejection_reason = reason
        return True


_library: EmergentSolutionLibrary | None = None


def get_library() -> EmergentSolutionLibrary:
    global _library
    if _library is None:
        _library = EmergentSolutionLibrary()
    return _library


def reset_library() -> None:
    global _library
    _library = None


__all__ = [
    "DiscoveredBy",
    "EmergentSolution",
    "EmergentSolutionLibrary",
    "EmergentSource",
    "SolutionStatus",
    "SourceKind",
    "get_library",
    "reset_library",
]
