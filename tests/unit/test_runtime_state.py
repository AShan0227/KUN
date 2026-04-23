"""RuntimeState tests."""

import pytest
from kun.datamodel.runtime import RuntimeState, StepRecord


@pytest.mark.unit
def test_accumulate_step_updates_aggregates():
    rt = RuntimeState(task_ref="tk-abc")
    rt.accumulate_step(
        StepRecord(
            step_id=1,
            skill_used="sk-foo",
            cost_usd_actual=0.01,
            cost_usd_equivalent=0.05,
            duration_sec=5.0,
            tokens_in=500,
            tokens_out=200,
        )
    )
    assert rt.current_step == 1
    assert rt.accumulated_cost_usd_actual == 0.01
    assert rt.accumulated_cost_usd_equivalent == 0.05
    assert rt.accumulated_tokens == 700


@pytest.mark.unit
def test_over_budget():
    rt = RuntimeState(task_ref="tk-abc")
    rt.accumulated_cost_usd_equivalent = 0.13
    assert rt.over_budget(estimated_cost_usd=0.10) is True  # 0.13 > 0.12
    assert rt.over_budget(estimated_cost_usd=0.20) is False
    assert rt.over_budget(estimated_cost_usd=0.0) is False
