from __future__ import annotations

import pytest
from kun.control_plane.frontier50 import (
    apply_frontier50_round_summary,
    build_frontier50_campaign_plan,
    initial_frontier50_work_item,
)
from kun.control_plane.qi_ab import QiABRoundSummary


def _task_ids(count: int = 50) -> list[str]:
    return [f"task-{index:02d}" for index in range(1, count + 1)]


def _summary(**overrides: object) -> QiABRoundSummary:
    payload: dict[str, object] = {
        "mission_id": "msn-frontier50",
        "task_plan_version": "v1",
        "round_id": "round-01",
        "work_item_id": "work-qi-ab-round-01",
        "task_ids": _task_ids(5),
        "answer_refs": [f"answer-{index}" for index in range(20)],
        "review_refs": [f"review-{index}" for index in range(45)],
        "report_ref": "report-01",
        "health_ref": "health-01",
        "kun_gate_passed": True,
        "kun_result_quality": 0.88,
    }
    payload.update(overrides)
    return QiABRoundSummary.model_validate(payload)


@pytest.mark.unit
def test_frontier50_campaign_builds_ten_rounds_and_initial_work_item() -> None:
    plan = build_frontier50_campaign_plan(
        mission_id="msn-frontier50",
        task_plan_version="v1",
        task_ids=_task_ids(),
    )

    assert len(plan.rounds) == 10
    assert plan.rounds[0].task_ids == _task_ids(5)
    assert plan.completed_round_count == 0

    work_item = initial_frontier50_work_item(plan)
    assert work_item.owner == "qi"
    assert work_item.work_item_id == "work-qi-ab-round-01"


@pytest.mark.unit
def test_frontier50_campaign_queues_next_round_only_after_pass() -> None:
    plan = build_frontier50_campaign_plan(
        mission_id="msn-frontier50",
        task_plan_version="v1",
        task_ids=_task_ids(),
    )

    decision = apply_frontier50_round_summary(plan=plan, summary=_summary())

    assert decision.plan.rounds[0].status == "passed"
    assert decision.queued_work_items[0].work_item_id == "work-qi-ab-round-02"
    assert decision.campaign_complete is False
    assert "next round" in decision.reason


@pytest.mark.unit
def test_frontier50_campaign_requires_same_task_retest_after_kun_repair() -> None:
    plan = build_frontier50_campaign_plan(
        mission_id="msn-frontier50",
        task_plan_version="v1",
        task_ids=_task_ids(),
    )

    decision = apply_frontier50_round_summary(
        plan=plan,
        summary=_summary(
            kun_gate_passed=False,
            kun_result_quality=0.71,
            repair_ticket_refs=["repair-ticket-1"],
        ),
    )

    assert decision.plan.rounds[0].status == "repairing"
    assert decision.plan.rounds[0].same_task_retest_required is True
    assert decision.queued_work_items[0].owner == "qi"
    assert decision.queued_work_items[0].work_item_id == "work-qi-ab-repair-round-01"


@pytest.mark.unit
def test_frontier50_campaign_marks_polluted_round_invalid_without_next_round() -> None:
    plan = build_frontier50_campaign_plan(
        mission_id="msn-frontier50",
        task_plan_version="v1",
        task_ids=_task_ids(),
    )

    decision = apply_frontier50_round_summary(
        plan=plan,
        summary=_summary(comparator_healthy=False),
    )

    assert decision.plan.rounds[0].status == "invalid"
    assert decision.plan.rounds[0].same_task_retest_required is True
    assert decision.queued_work_items[0].owner == "nuo"
    assert decision.round_contract.agent_failure_counted is False
    assert not decision.round_contract.next_round_allowed


@pytest.mark.unit
def test_frontier50_campaign_rejects_non_even_round_split() -> None:
    with pytest.raises(ValueError, match="divide evenly"):
        build_frontier50_campaign_plan(
            mission_id="msn-frontier50",
            task_plan_version="v1",
            task_ids=_task_ids(49),
        )
