"""C16 task flow API helpers."""

from __future__ import annotations

from kun.api.task_control import _build_flow_steps
from kun.core.orm import RuntimeStateRow, TaskRow


def _task() -> TaskRow:
    return TaskRow(
        task_id="tk-flow",
        tenant_id="u-sylvan",
        fingerprint="sha256:" + "a" * 64,
        task_type="coding.python",
        risk_level="low",
        complexity_score=0.2,
        estimated_cost_usd=0.1,
        estimated_duration_sec=30,
        success_criteria_short="跑完测试",
        version=1,
    )


def test_build_flow_steps_uses_completed_and_pending_steps() -> None:
    runtime = RuntimeStateRow(
        state_id="rt-1",
        task_ref="tk-flow",
        tenant_id="u-sylvan",
        current_step=1,
        total_planned_steps=3,
        status="running",
        accumulated_cost_usd_actual=0.0,
        accumulated_cost_usd_equivalent=0.0,
        accumulated_tokens=0,
        failures_this_run=0,
        blob={
            "completed_steps": [
                {
                    "step_id": 1,
                    "skill_used": "pytest",
                    "output_ref": "all green",
                    "cost_usd_equivalent": 0.02,
                    "duration_sec": 1.5,
                }
            ],
            "next_step_plan": {"skill": "review", "input_preview": "检查 diff"},
        },
    )

    steps = _build_flow_steps(_task(), runtime)

    assert [step.status for step in steps] == ["done", "running", "pending"]
    assert steps[0].title == "pytest"
    assert steps[1].title == "review"
    assert steps[1].input == "检查 diff"
    assert steps[2].deps == ["2"]


def test_build_flow_steps_falls_back_without_runtime() -> None:
    steps = _build_flow_steps(_task(), None)

    assert len(steps) == 1
    assert steps[0].status == "pending"
    assert steps[0].title == "step 1"
