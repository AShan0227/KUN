"""OODA 外层循环测试."""

from __future__ import annotations

import pytest
from kun.core.ooda_loop import OODACycle, OODAEngine, OODAState


@pytest.mark.asyncio
async def test_initial_cycle_starts_at_observe() -> None:
    cycle = OODACycle(task_ref="task-1")

    assert cycle.current_state == OODAState.OBSERVE
    assert cycle.state_history[0][0] == OODAState.OBSERVE
    assert cycle.cycle_id.startswith("ooda-")


@pytest.mark.asyncio
async def test_valid_transition_records_orientation_payload() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1")

    oriented = await engine.transition(cycle, OODAState.ORIENT, {"summary": "用户要审 PR"})

    assert oriented.current_state == OODAState.ORIENT
    assert oriented.orientation == {"summary": "用户要审 PR"}
    assert oriented.state_history[-1][0] == OODAState.ORIENT


@pytest.mark.asyncio
async def test_illegal_transition_is_rejected() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1")

    with pytest.raises(ValueError, match="illegal OODA transition"):
        await engine.transition(cycle, OODAState.ACT, {"tool": "pytest"})


@pytest.mark.asyncio
async def test_full_happy_path_can_finish_done() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1")

    cycle = await engine.transition(cycle, OODAState.ORIENT, {"summary": "修 lint"})
    cycle = await engine.transition(cycle, OODAState.DECIDE, {"expected_outcome": "green"})
    cycle = await engine.transition(cycle, OODAState.ACT, {"status": "done", "outcome": "green"})
    reflection = await engine.reflect(cycle)
    cycle = await engine.transition(cycle, OODAState.REFLECT, reflection)
    cycle = await engine.transition(cycle, OODAState.DONE, {"result": "merged"})

    assert cycle.current_state == OODAState.DONE
    assert cycle.reflections[-1]["needs_adjust"] is False
    assert cycle.metadata["done"] == {"result": "merged"}


@pytest.mark.asyncio
async def test_reflect_can_continue_to_next_decision() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1")

    cycle = await engine.transition(cycle, OODAState.ORIENT, {"summary": "两步任务"})
    cycle = await engine.transition(cycle, OODAState.DECIDE, {"expected_outcome": "done"})
    cycle = await engine.transition(cycle, OODAState.ACT, {"status": "done", "outcome": "done"})
    cycle = await engine.transition(cycle, OODAState.REFLECT, await engine.reflect(cycle))
    cycle = await engine.transition(
        cycle,
        OODAState.DECIDE,
        {"expected_outcome": "done", "reason": "continue_next_step"},
    )

    assert cycle.current_state == OODAState.DECIDE
    assert cycle.decision is not None
    assert cycle.decision["reason"] == "continue_next_step"


@pytest.mark.asyncio
async def test_done_is_terminal() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1", current_state=OODAState.DONE)

    with pytest.raises(ValueError, match="illegal OODA transition"):
        await engine.transition(cycle, OODAState.OBSERVE, {})


@pytest.mark.asyncio
async def test_reflect_requests_adjust_when_no_action_exists() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1", current_state=OODAState.ACT)

    reflection = await engine.reflect(cycle)

    assert reflection["needs_adjust"] is True
    assert reflection["reason"] == "no action has been recorded"


@pytest.mark.asyncio
async def test_reflect_detects_failed_action() -> None:
    engine = OODAEngine()
    cycle = OODACycle(
        task_ref="task-1",
        current_state=OODAState.ACT,
        decision={"expected_outcome": "green"},
        actions_taken=[{"status": "failed", "outcome": "red"}],
    )

    reflection = await engine.reflect(cycle)

    assert reflection["needs_adjust"] is True
    assert reflection["reason"] == "latest action failed"


@pytest.mark.asyncio
async def test_reflect_detects_outcome_mismatch() -> None:
    engine = OODAEngine()
    cycle = OODACycle(
        task_ref="task-1",
        current_state=OODAState.ACT,
        decision={"expected_outcome": "green"},
        actions_taken=[{"status": "done", "outcome": "yellow"}],
    )

    reflection = await engine.reflect(cycle)

    assert reflection["needs_adjust"] is True
    assert reflection["reason"] == "latest action outcome differed from decision"


@pytest.mark.asyncio
async def test_should_adjust_uses_latest_reflection() -> None:
    engine = OODAEngine()
    cycle = OODACycle(
        task_ref="task-1",
        current_state=OODAState.REFLECT,
        reflections=[{"needs_adjust": True, "reason": "budget drift"}],
    )

    assert await engine.should_adjust(cycle) is True


@pytest.mark.asyncio
async def test_adjust_records_adjustment_and_returns_to_decide() -> None:
    engine = OODAEngine()
    cycle = OODACycle(
        task_ref="task-1",
        current_state=OODAState.REFLECT,
        decision={"expected_outcome": "green", "plan": "A"},
        reflections=[{"needs_adjust": True, "reason": "tool failed"}],
    )

    adjusted = await engine.adjust(cycle)

    assert adjusted.current_state == OODAState.DECIDE
    assert adjusted.adjustments[0]["reason"] == "tool failed"
    assert adjusted.decision is not None
    assert adjusted.decision["adjusted"] is True
    assert adjusted.decision["adjust_reason"] == "tool failed"
    assert [state for state, _ in adjusted.state_history[-2:]] == [
        OODAState.ADJUST,
        OODAState.DECIDE,
    ]


@pytest.mark.asyncio
async def test_adjust_rejects_when_not_in_reflect_state() -> None:
    engine = OODAEngine()
    cycle = OODACycle(task_ref="task-1", current_state=OODAState.ACT)

    with pytest.raises(ValueError, match="reflect state"):
        await engine.adjust(cycle)


@pytest.mark.asyncio
async def test_adjust_rejects_when_reflection_says_no_adjust() -> None:
    engine = OODAEngine()
    cycle = OODACycle(
        task_ref="task-1",
        current_state=OODAState.REFLECT,
        reflections=[{"needs_adjust": False, "reason": "all good"}],
    )

    with pytest.raises(ValueError, match="does not need adjustment"):
        await engine.adjust(cycle)
