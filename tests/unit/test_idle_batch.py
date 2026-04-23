"""idle-batch scheduler tests."""

from typing import Any

import pytest
from kun.engineering.idle_batch import (
    IdleBatchStep,
    list_steps,
    register_step,
    run_once,
)


class _RecordingStep(IdleBatchStep):
    step_id = "test_recorder"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, tenant_id: str) -> dict[str, Any]:
        self.calls += 1
        return {"tenant": tenant_id, "calls": self.calls}


@pytest.mark.unit
def test_default_steps_registered():
    steps = list_steps()
    assert "health_report" in steps
    assert "task_replay" in steps
    assert "route_rule_mining" in steps


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_once_enabled_filter():
    recorder = _RecordingStep()
    register_step(recorder)
    reports = await run_once("u-test", enabled={"test_recorder"})
    assert len(reports) == 1
    assert reports[0].step_id == "test_recorder"
    assert reports[0].status == "ok"
    assert recorder.calls == 1
    assert reports[0].summary == {"tenant": "u-test", "calls": 1}
