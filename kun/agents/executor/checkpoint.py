"""Task checkpoint service (LT.C — long-task 持久化 + resume).

Executor 每完成一步 → checkpoint_writer(snapshot) → 落库.
进程挂掉 / restart → checkpoint_reader.latest_active(task_id) → resume.

设计要点 (ADR-024 frozen_dataclass 模式):
  - TaskCheckpoint frozen dataclass — 跨 process 序列化友好
  - to_row_payload(tenant_id) → dict for ORM writer (service 不导 sqlalchemy)
  - writer / reader 都是 callback (DI), 测试用 fake, prod 接真 session_scope
  - sequence 单调递增 — 不依赖时间戳防时钟跳变

resume 语义:
  - 取 status='active' AND task_id=X 的最大 sequence row
  - 若该 row 的 goal_anchor_id 已经不再匹配最新 anchor → status 改 'failed_resume',
    caller 决定是否重新跑 (避免按旧 anchor resume 拿到错 context)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.executor.checkpoint")


CheckpointStatus = Literal["active", "final", "failed_resume"]


@dataclass(frozen=True)
class TaskCheckpoint:
    """长任务单步 checkpoint snapshot."""

    checkpoint_id: str
    task_id: str
    step_idx: int
    sequence: int
    conversation_snapshot: list[dict[str, Any]]
    working_state: dict[str, Any]
    artifact_refs: list[str]
    goal_anchor_id: str | None = None
    last_self_report: dict[str, Any] | None = None
    cost_usd_so_far: float = 0.0
    tokens_used_so_far: int = 0
    status: CheckpointStatus = "active"
    rationale: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        """转 ORM 可写 dict (service 不导 sqlalchemy)."""
        return {
            "tenant_id": tenant_id,
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "step_idx": self.step_idx,
            "sequence": self.sequence,
            "conversation_snapshot": list(self.conversation_snapshot),
            "working_state": dict(self.working_state),
            "artifact_refs": list(self.artifact_refs),
            "goal_anchor_id": self.goal_anchor_id,
            "last_self_report": (
                dict(self.last_self_report)
                if self.last_self_report is not None
                else None
            ),
            "cost_usd_so_far": float(self.cost_usd_so_far),
            "tokens_used_so_far": int(self.tokens_used_so_far),
            "status": self.status,
            "rationale": self.rationale,
            "created_at": self.created_at,
        }


# Writer: 拿 row_payload dict, 落库. 测试用 fake; prod 接 session_scope insert.
CheckpointWriter = Callable[[dict[str, Any]], Awaitable[None]]

# Reader: 给 (tenant_id, task_id) 返回最新 active checkpoint (或 None).
CheckpointReader = Callable[[str, str], Awaitable["TaskCheckpoint | None"]]

# Status marker: 把指定 checkpoint 标 status (用于 finalize / mark failed_resume).
CheckpointStatusMarker = Callable[
    [str, str, CheckpointStatus], Awaitable[None]
]
"""(tenant_id, checkpoint_id, new_status) → None."""


class TaskCheckpointService:
    """Executor 侧 checkpoint 收发 service."""

    def __init__(
        self,
        *,
        writer: CheckpointWriter,
        reader: CheckpointReader,
        status_marker: CheckpointStatusMarker | None = None,
    ) -> None:
        self._writer = writer
        self._reader = reader
        self._status_marker = status_marker
        # 每 task 的 sequence counter (in-memory; resume 时从 DB 拉过去最大值)
        # 存的是"已用过的最大 sequence", 下次 save 返回 stored+1.
        self._sequences: dict[str, int] = {}

    def _advance_sequence(self, task_id: str) -> int:
        """save() 调: 取下一个 sequence + advance internal counter."""
        last = self._sequences.get(task_id, -1)
        nxt = last + 1
        self._sequences[task_id] = nxt
        return nxt

    def _set_resume_baseline(self, task_id: str, last_sequence: int) -> None:
        """resume() 调: 把 internal counter 顶到 cp.sequence, 下次 save +1."""
        existing = self._sequences.get(task_id, -1)
        self._sequences[task_id] = max(existing, last_sequence)

    async def save(
        self,
        *,
        tenant_id: str,
        task_id: str,
        step_idx: int,
        conversation_snapshot: list[dict[str, Any]],
        working_state: dict[str, Any] | None = None,
        artifact_refs: list[str] | None = None,
        goal_anchor_id: str | None = None,
        last_self_report: dict[str, Any] | None = None,
        cost_usd_so_far: float = 0.0,
        tokens_used_so_far: int = 0,
        status: CheckpointStatus = "active",
        rationale: str = "",
    ) -> TaskCheckpoint:
        """落一个 checkpoint. 返回 frozen TaskCheckpoint (含 id + sequence).

        Writer 异常上抛 — checkpoint 失败是状态机推进失败, 必须可见
        (ADR-024 frozen_dataclass_agent_io_contract: 状态机推进失败 raise).
        """
        sequence = self._advance_sequence(task_id)
        cp = TaskCheckpoint(
            checkpoint_id=new_id("task_checkpoint"),
            task_id=task_id,
            step_idx=step_idx,
            sequence=sequence,
            conversation_snapshot=list(conversation_snapshot),
            working_state=dict(working_state or {}),
            artifact_refs=list(artifact_refs or []),
            goal_anchor_id=goal_anchor_id,
            last_self_report=(
                dict(last_self_report) if last_self_report is not None else None
            ),
            cost_usd_so_far=cost_usd_so_far,
            tokens_used_so_far=tokens_used_so_far,
            status=status,
            rationale=rationale,
        )
        await self._writer(cp.to_row_payload(tenant_id))
        log.info(
            "task_checkpoint.saved",
            task_id=task_id,
            checkpoint_id=cp.checkpoint_id,
            step_idx=step_idx,
            sequence=sequence,
            status=status,
        )
        return cp

    async def resume(
        self,
        *,
        tenant_id: str,
        task_id: str,
        current_anchor_id: str | None = None,
    ) -> TaskCheckpoint | None:
        """从 DB 拉最新 active checkpoint. 若 anchor 不匹配 → 标 failed_resume, 返 None.

        Returns:
          TaskCheckpoint when safely resumable;
          None when no checkpoint OR anchor mismatch (stale resume).
        """
        cp = await self._reader(tenant_id, task_id)
        if cp is None:
            log.info(
                "task_checkpoint.no_active_found", task_id=task_id
            )
            return None
        if (
            current_anchor_id is not None
            and cp.goal_anchor_id is not None
            and cp.goal_anchor_id != current_anchor_id
        ):
            log.warning(
                "task_checkpoint.anchor_mismatch_marking_failed_resume",
                task_id=task_id,
                checkpoint_id=cp.checkpoint_id,
                cp_anchor=cp.goal_anchor_id,
                current_anchor=current_anchor_id,
            )
            if self._status_marker is not None:
                try:
                    await self._status_marker(
                        tenant_id, cp.checkpoint_id, "failed_resume"
                    )
                except Exception as e:  # status update 是状态累积, 失败仅 log
                    log.warning(
                        "task_checkpoint.status_marker_failed",
                        error=str(e),
                    )
            return None
        # safe to resume — set baseline so next save() gets sequence = cp.sequence + 1
        self._set_resume_baseline(task_id, cp.sequence)
        log.info(
            "task_checkpoint.resumed",
            task_id=task_id,
            checkpoint_id=cp.checkpoint_id,
            sequence=cp.sequence,
            step_idx=cp.step_idx,
        )
        return cp

    async def finalize(
        self,
        *,
        tenant_id: str,
        checkpoint_id: str,
    ) -> None:
        """任务正常完成 → 标 final.

        status 状态机推进 — 失败 raise.
        """
        if self._status_marker is None:
            raise RuntimeError(
                "finalize requires status_marker; none provided to service"
            )
        await self._status_marker(tenant_id, checkpoint_id, "final")
        log.info(
            "task_checkpoint.finalized",
            checkpoint_id=checkpoint_id,
        )


__all__ = [
    "CheckpointReader",
    "CheckpointStatus",
    "CheckpointStatusMarker",
    "CheckpointWriter",
    "TaskCheckpoint",
    "TaskCheckpointService",
]
