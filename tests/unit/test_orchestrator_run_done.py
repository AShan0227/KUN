"""Orchestrator.run() terminal-event handling (audit F027).

The long-task path could surface a second "done" event with a different shape
({...,"cancelled":...}, no "result") ahead of the canonical one. run() must
consume only the canonical done (the one carrying a TaskResult under "result")
and must not crash on the non-canonical one.
"""

from __future__ import annotations

import pytest
from kun.engineering.orchestrator import Orchestrator, OrchestratorEvent, TaskResult


def _canonical(task_id: str) -> dict:
    return {
        "result": TaskResult(task_id=task_id, status="done", answer="ok").model_dump(mode="json")
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_ignores_non_canonical_done_and_returns_canonical() -> None:
    # Bypass the heavy __init__ — run() only touches self.stream.
    orch = Orchestrator.__new__(Orchestrator)

    async def fake_stream(user_message: str, *, output_kind: str = "user"):
        yield OrchestratorEvent(kind="answer", data={"content": "x"})
        # LT-shaped done WITHOUT "result" — used to crash run() with KeyError
        yield OrchestratorEvent(kind="done", data={"cancelled": False})
        yield OrchestratorEvent(kind="answer", data={"content": "x", "task_id": "t1"})
        yield OrchestratorEvent(kind="done", data=_canonical("t1"))

    orch.stream = fake_stream  # type: ignore[method-assign]
    result = await Orchestrator.run(orch, "hello")
    assert result.task_id == "t1"
    assert result.status == "done"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_raises_when_no_canonical_done() -> None:
    orch = Orchestrator.__new__(Orchestrator)

    async def fake_stream(user_message: str, *, output_kind: str = "user"):
        yield OrchestratorEvent(kind="answer", data={"content": "x"})
        yield OrchestratorEvent(kind="done", data={"cancelled": False})  # no result

    orch.stream = fake_stream  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="without a done event"):
        await Orchestrator.run(orch, "hello")
