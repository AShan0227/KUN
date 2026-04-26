"""Multi-task non-blocking scheduler (C18).

Standalone runtime primitive. Chat/API wiring is intentionally left for the M4
wire pass.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.task import TaskRef

SchedulerStatus = Literal["queued", "running", "done", "failed", "cancelled"]
TaskRunner = Callable[[TaskRef], Awaitable[Any]]


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["done", "failed", "cancelled"]
    output: Any = None
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskStatusSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    user_id: str
    status: SchedulerStatus
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    queue_position: int | None = None
    error: str = ""


class _TaskRecord:
    def __init__(self, task: TaskRef, future: asyncio.Future[TaskResult]) -> None:
        self.task = task
        self.task_id = task.meta.task_id
        self.user_id = task.meta.owner.user_id or task.meta.owner.tenant_id
        self.status: SchedulerStatus = "queued"
        self.queued_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error = ""
        self.future = future
        self.worker: asyncio.Task[None] | None = None


class MultiTaskScheduler:
    """Fair-ish in-memory scheduler with per-user and global concurrency caps."""

    def __init__(
        self,
        max_concurrent_per_user: int = 3,
        max_concurrent_global: int = 50,
        *,
        runner: TaskRunner | None = None,
    ) -> None:
        if max_concurrent_per_user <= 0:
            raise ValueError("max_concurrent_per_user must be positive")
        if max_concurrent_global <= 0:
            raise ValueError("max_concurrent_global must be positive")
        self.max_concurrent_per_user = max_concurrent_per_user
        self.max_concurrent_global = max_concurrent_global
        self._runner = runner or _default_runner
        self._records: dict[str, _TaskRecord] = {}
        self._queue: deque[str] = deque()
        self._running_global = 0
        self._running_by_user: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def submit(self, task: TaskRef) -> str:
        """Submit a task. Over-capacity tasks wait in FIFO queue."""

        future: asyncio.Future[TaskResult] = asyncio.get_running_loop().create_future()
        record = _TaskRecord(task, future)
        async with self._lock:
            if record.task_id in self._records:
                raise ValueError(f"task already submitted: {record.task_id}")
            self._records[record.task_id] = record
            self._queue.append(record.task_id)
            self._pump_locked()
        return record.task_id

    async def wait_done(self, task_id: str, timeout_sec: int) -> TaskResult:
        """Wait for a task to finish."""

        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"unknown task: {task_id}")
        return await asyncio.wait_for(asyncio.shield(record.future), timeout=timeout_sec)

    def cancel(self, task_id: str, reason: str) -> bool:
        """Cancel a queued or running task."""

        record = self._records.get(task_id)
        if record is None or record.status in {"done", "failed", "cancelled"}:
            return False
        record.error = reason
        if record.status == "queued":
            with suppress(ValueError):
                self._queue.remove(task_id)
            self._finish_cancelled(record)
            return True
        if record.worker is not None:
            record.worker.cancel()
            return True
        return False

    def get_status(self, task_id: str) -> TaskStatusSnapshot:
        """Return queued/running/done/failed/cancelled."""

        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"unknown task: {task_id}")
        return self._snapshot(record)

    def list_user_tasks(self, user_id: str) -> list[TaskStatusSnapshot]:
        """List all tasks owned by a user."""

        return [
            self._snapshot(record) for record in self._records.values() if record.user_id == user_id
        ]

    def _pump_locked(self) -> None:
        while self._queue and self._running_global < self.max_concurrent_global:
            started = False
            for task_id in list(self._queue):
                record = self._records[task_id]
                user_running = self._running_by_user.get(record.user_id, 0)
                if user_running >= self.max_concurrent_per_user:
                    continue
                self._queue.remove(task_id)
                self._start_locked(record)
                started = True
                break
            if not started:
                break

    def _start_locked(self, record: _TaskRecord) -> None:
        record.status = "running"
        record.started_at = datetime.now(UTC)
        self._running_global += 1
        self._running_by_user[record.user_id] = self._running_by_user.get(record.user_id, 0) + 1
        record.worker = asyncio.create_task(self._run_record(record))

    async def _run_record(self, record: _TaskRecord) -> None:
        try:
            output = await self._runner(record.task)
        except asyncio.CancelledError:
            self._finish_cancelled(record)
            raise
        except Exception as e:
            record.status = "failed"
            record.error = f"{type(e).__name__}: {e}"
            result = TaskResult(
                task_id=record.task_id,
                status="failed",
                error=record.error,
                started_at=record.started_at,
            )
            self._finish(record, result)
        else:
            result = TaskResult(
                task_id=record.task_id,
                status="done",
                output=output,
                started_at=record.started_at,
            )
            self._finish(record, result)
        finally:
            async with self._lock:
                self._running_global = max(0, self._running_global - 1)
                self._running_by_user[record.user_id] = max(
                    0,
                    self._running_by_user.get(record.user_id, 0) - 1,
                )
                self._pump_locked()

    def _finish(self, record: _TaskRecord, result: TaskResult) -> None:
        record.status = result.status
        record.finished_at = result.finished_at
        if not record.future.done():
            record.future.set_result(result)

    def _finish_cancelled(self, record: _TaskRecord) -> None:
        result = TaskResult(
            task_id=record.task_id,
            status="cancelled",
            error=record.error or "cancelled",
            started_at=record.started_at,
        )
        self._finish(record, result)

    def _snapshot(self, record: _TaskRecord) -> TaskStatusSnapshot:
        queue_position: int | None = None
        if record.status == "queued":
            try:
                queue_position = list(self._queue).index(record.task_id) + 1
            except ValueError:
                queue_position = None
        return TaskStatusSnapshot(
            task_id=record.task_id,
            user_id=record.user_id,
            status=record.status,
            queued_at=record.queued_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            queue_position=queue_position,
            error=record.error,
        )


async def _default_runner(task: TaskRef) -> dict[str, str]:
    return {"task_id": task.meta.task_id, "summary": task.l1_summary()}


__all__ = [
    "MultiTaskScheduler",
    "SchedulerStatus",
    "TaskResult",
    "TaskStatusSnapshot",
]
