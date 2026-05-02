"""注意力预算守门器 (BATCH4 C8 / T57)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from kun.core.ids import new_id

AgentStatus = Literal["queued", "running", "blocked", "done", "failed"]


@dataclass(frozen=True)
class AgentSnapshot:
    agent_id: str
    task_id: str
    status: AgentStatus
    goal: str
    current_step: str = ""
    progress_pct: float = 0.0
    cost_usd: float = 0.0
    risk_level: str = "low"


@dataclass(frozen=True)
class QueuedTask:
    queue_id: str
    user_id: str
    task_meta: dict[str, Any]
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AttentionBudgetGuard:
    """控制同一用户同时看到/跑的 agent 数量, 并强制摘要输出变短."""

    def __init__(self, max_active_sessions_default: int = 3) -> None:
        if max_active_sessions_default < 1:
            raise ValueError("max_active_sessions_default must be >= 1")
        self.max_active_sessions_default = max_active_sessions_default
        self._active_sessions: dict[str, set[str]] = {}
        self._user_limits: dict[str, int] = {}
        self._queues: dict[str, list[QueuedTask]] = {}

    def set_user_limit(self, user_id: str, max_active_sessions: int) -> None:
        if max_active_sessions < 1:
            raise ValueError("max_active_sessions must be >= 1")
        self._user_limits[user_id] = max_active_sessions

    def register_session(self, user_id: str, session_id: str) -> bool:
        """尝试登记一个活跃 session. 超限返回 False."""

        if not self.can_start_session(user_id):
            return False
        self._active_sessions.setdefault(user_id, set()).add(session_id)
        return True

    def end_session(self, user_id: str, session_id: str) -> None:
        sessions = self._active_sessions.get(user_id)
        if sessions is None:
            return
        sessions.discard(session_id)
        if not sessions:
            self._active_sessions.pop(user_id, None)

    def can_start_session(self, user_id: str) -> bool:
        """检查是否能起新 session."""

        return self.active_count(user_id) < self._limit_for(user_id)

    def queue_excess(self, user_id: str, task_meta: dict[str, Any]) -> str:
        """超过上限自动入队, 返回 queue_id."""

        queue_id = f"queue-{new_id('task')}"
        item = QueuedTask(queue_id=queue_id, user_id=user_id, task_meta=dict(task_meta))
        self._queues.setdefault(user_id, []).append(item)
        return queue_id

    def pop_next_queued(self, user_id: str) -> QueuedTask | None:
        queue = self._queues.get(user_id) or []
        if not queue:
            return None
        item = queue.pop(0)
        if not queue:
            self._queues.pop(user_id, None)
        return item

    def queue_depth(self, user_id: str) -> int:
        return len(self._queues.get(user_id, []))

    def active_count(self, user_id: str) -> int:
        return len(self._active_sessions.get(user_id, set()))

    def summarize_agent_status(self, agents: list[AgentSnapshot]) -> str:
        """每个 agent 最多 5 行摘要, 避免用户被并发输出淹没."""

        if not agents:
            return "当前没有活跃 agent。"
        blocks = [self._summarize_one(agent) for agent in agents]
        return "\n\n".join(blocks)

    def _summarize_one(self, agent: AgentSnapshot) -> str:
        lines = [
            f"{agent.agent_id} · {agent.status} · {agent.goal}",
            f"任务: {agent.task_id}",
            f"进度: {agent.progress_pct:.0%} · 当前: {agent.current_step or '等待下一步'}",
            f"成本: ${agent.cost_usd:.4f} · 风险: {agent.risk_level}",
        ]
        return "\n".join(lines[:5])

    def _limit_for(self, user_id: str) -> int:
        return self._user_limits.get(user_id, self.max_active_sessions_default)
