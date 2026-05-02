"""C20 dynamic local replanning tests."""

import pytest
from kun.brain.planner import ExecutionPlan, PlanStep
from kun.core.ooda_loop import OODACycle
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.dynamic_replan import DynamicReplanner, ReplanDecision


def _task() -> TaskRef:
    owner = Owner(tenant_id="tenant-1", user_id="user-1")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("ship report", owner),
        task_type="ops.report",
        owner=owner,
        estimated_cost_usd=0.9,
        estimated_duration_sec=90,
        success_criteria_short="ship report",
    )
    return TaskRef(meta=meta, spec=TaskSpec(goal_detail="ship report"))


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        task_ref=_task(),
        steps=[
            PlanStep(step_id=1, description="collect data", estimated_cost_usd=0.2),
            PlanStep(
                step_id=2,
                description="draft report",
                depends_on=[1],
                estimated_cost_usd=0.3,
            ),
            PlanStep(
                step_id=3,
                description="send report",
                depends_on=[2],
                estimated_cost_usd=0.4,
            ),
        ],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_replan_from_metadata_request() -> None:
    cycle = OODACycle(
        task_ref="task-1",
        metadata={"replan_requested": True, "replan_reason": "user changed goal"},
    )

    yes, reason = await DynamicReplanner().detect_replan_needed(cycle)

    assert yes is True
    assert reason == "user changed goal"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_replan_from_observation() -> None:
    cycle = OODACycle(task_ref="task-1", observations=[{"needs_replan": True}])

    yes, reason = await DynamicReplanner().detect_replan_needed(cycle)

    assert yes is True
    assert reason == "observation requested replan"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_replan_from_reflection() -> None:
    cycle = OODACycle(
        task_ref="task-1",
        reflections=[{"needs_adjust": True, "reason": "quality drift"}],
    )

    yes, reason = await DynamicReplanner().detect_replan_needed(cycle)

    assert yes is True
    assert reason == "quality drift"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_replan_from_budget_exceeded() -> None:
    cycle = OODACycle(task_ref="task-1", metadata={"budget": 1.0, "spent": 1.2})

    yes, reason = await DynamicReplanner().detect_replan_needed(cycle)

    assert yes is True
    assert reason == "budget exceeded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_no_replan_for_healthy_cycle() -> None:
    yes, reason = await DynamicReplanner().detect_replan_needed(OODACycle(task_ref="task-1"))

    assert yes is False
    assert reason == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replan_preserves_done_steps_and_rechains_deps() -> None:
    replanned = await DynamicReplanner().replan_from_step(
        _plan(),
        1,
        [
            {
                "replacement_steps": [
                    {"description": "redraft with latest data", "skill_hint": "writing"},
                    {"description": "validate with owner", "skill_hint": "review"},
                ]
            }
        ],
    )

    assert [step.description for step in replanned.steps] == [
        "collect data",
        "draft report",
        "redraft with latest data",
        "validate with owner",
    ]
    assert replanned.steps[2].step_id == 3
    assert replanned.steps[2].depends_on == [2]
    assert replanned.steps[3].depends_on == [3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replan_fallback_adds_adjust_and_validation_steps() -> None:
    replanned = await DynamicReplanner().replan_from_step(
        _plan(),
        0,
        [{"status": "blocked", "reason": "source data missing"}],
    )

    assert len(replanned.steps) == 3
    assert "source data missing" in replanned.steps[1].description
    assert replanned.steps[2].skill_hint == "task.validation"


@pytest.mark.unit
def test_sunk_cost_is_precise_and_inclusive() -> None:
    sunk = DynamicReplanner().calculate_sunk_cost(_plan(), 1)

    assert sunk == 0.5


@pytest.mark.unit
def test_sunk_cost_estimate_reports_progress() -> None:
    estimate = DynamicReplanner().estimate_sunk_cost(_plan(), 1)

    assert estimate.completed_steps == 2
    assert estimate.total_planned_steps == 3
    assert estimate.completed_cost_usd == 0.5
    assert estimate.progress_ratio == pytest.approx(2 / 3)
    assert estimate.can_reuse_outputs is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replan_result_reports_bookkeeping() -> None:
    result = await DynamicReplanner().replan_with_result(
        _plan(),
        1,
        [{"replacement_steps": ["use backup data"]}],
        reason="primary data source failed",
    )

    assert result.reason == "primary data source failed"
    assert result.sunk_cost_usd == 0.5
    assert result.preserved_step_ids == [1, 2]
    assert result.replacement_step_ids == [3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_replan_decision_compat_wrapper() -> None:
    decision = await DynamicReplanner().detect_replan_decision(
        OODACycle(task_ref="task-1", metadata={"budget": 1.0, "spent": 2.0})
    )

    assert decision.needs_replan is True
    assert decision.reason == "budget exceeded"
    assert decision.confidence >= 0.85


@pytest.mark.unit
def test_replan_roi_gate_compat_method() -> None:
    estimate = DynamicReplanner().estimate_sunk_cost(_plan(), 0)

    worth_it, reason = DynamicReplanner().is_replan_worth_it(
        ReplanDecision(needs_replan=True, reason="blocked", confidence=0.9),
        estimate,
    )

    assert worth_it is True
    assert reason == "high_confidence_signal"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replan_rejects_empty_plan() -> None:
    with pytest.raises(ValueError, match="empty"):
        await DynamicReplanner().replan_from_step(ExecutionPlan(task_ref=_task()), 0, [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replan_rejects_bad_step_index() -> None:
    with pytest.raises(IndexError, match="out of range"):
        await DynamicReplanner().replan_from_step(_plan(), 9, [])
