"""WebSocket runtime helper tests."""

from __future__ import annotations

import asyncio

import pytest
from kun.api.ws import _cancel_task, _clear_finished_task


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_task_stops_running_task() -> None:
    cancelled = asyncio.Event()

    async def run_forever() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(run_forever())
    await asyncio.sleep(0)

    await _cancel_task(task)

    assert task.cancelled()
    assert cancelled.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_finished_task_returns_none_for_completed_task() -> None:
    async def done() -> None:
        return None

    task = asyncio.create_task(done())
    await task

    assert _clear_finished_task(task) is None
