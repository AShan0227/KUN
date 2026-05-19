from __future__ import annotations

import pytest
from kun.control_plane.qi_ab import (
    QI_AB_EXPECTED_ANSWER_COUNT,
    QI_AB_EXPECTED_REVIEW_COUNT,
    QiABRoundSummary,
    build_qi_ab_round_contract,
    build_qi_ab_round_work_item,
)

pytestmark = pytest.mark.unit


def _refs(prefix: str, count: int) -> list[str]:
    return [f"artifact-{prefix}-{index:02d}" for index in range(1, count + 1)]


def _summary(**overrides: object) -> QiABRoundSummary:
    payload: dict[str, object] = {
        "mission_id": "msn-qi-ab",
        "task_plan_version": "v6",
        "round_id": "round-01",
        "work_item_id": "work-qi-ab-round-01",
        "task_ids": [f"abfrontier-{index:04d}" for index in range(1, 6)],
        "answer_refs": _refs("answer", QI_AB_EXPECTED_ANSWER_COUNT),
        "review_refs": _refs("review", QI_AB_EXPECTED_REVIEW_COUNT),
        "report_ref": "artifact-report-round-01",
        "health_ref": "artifact-health-round-01",
        "repair_ticket_refs": [],
        "comparator_healthy": True,
        "kun_gate_passed": True,
        "kun_result_quality": 0.91,
        "speed": 0.72,
        "cost": 0.68,
    }
    payload.update(overrides)
    return QiABRoundSummary.model_validate(payload)


def test_frontier50_round_is_control_plane_work_item_only() -> None:
    item = build_qi_ab_round_work_item(
        mission_id="msn-qi-ab",
        task_plan_version="v6",
        round_id="round-01",
        task_ids=["task-1", "task-2", "task-3", "task-4", "task-5"],
    )

    assert item.work_item_id == "work-qi-ab-round-01"
    assert item.type == "test"
    assert item.owner == "qi"
    assert item.status == "queued"
    assert "20 answers" in item.expected_output
    assert "45 reviews" in item.expected_output
    assert "No external command" in item.expected_output


def test_passed_round_records_manifest_gate_and_allows_next_round() -> None:
    contract = build_qi_ab_round_contract(
        _summary(),
        next_round_id="round-02",
        next_round_task_ids=["abfrontier-0006", "abfrontier-0007"],
    )

    assert contract.verdict == "pass"
    assert contract.round_valid is True
    assert contract.agent_failure_counted is False
    assert contract.next_round_allowed is True
    assert contract.hard_gate_failures == []
    assert contract.repair_work_item is None
    assert contract.next_round_work_item is not None
    assert contract.next_round_work_item.work_item_id == "work-qi-ab-round-02"
    assert contract.next_round_work_item.dependencies == ["work-qi-ab-round-01"]
    assert contract.work_item_result.status == "done"

    manifest = contract.artifact_manifest
    assert manifest.kind == "run"
    assert len(manifest.artifact_refs) == 67
    assert manifest.primary_artifact_ref == "artifact-report-round-01"
    assert manifest.evidence_refs == ["artifact-health-round-01"]
    assert len(manifest.review_refs) == 45

    gate = contract.gate_evaluation
    assert gate.north_star_verdict == "pass"
    assert gate.next_action == "continue"
    assert gate.next_state == "running"
    assert gate.failure_category is None
    assert gate.score_breakdown["answer_count"] == 20.0
    assert gate.score_breakdown["review_count"] == 45.0
    assert gate.score_breakdown["next_round_allowed"] == 1.0


def test_comparator_unhealthy_marks_round_invalid_without_agent_failure() -> None:
    contract = build_qi_ab_round_contract(
        _summary(comparator_healthy=False),
        next_round_id="round-02",
        next_round_task_ids=["abfrontier-0006"],
    )

    assert contract.verdict == "invalid"
    assert contract.round_valid is False
    assert contract.agent_failure_counted is False
    assert contract.next_round_allowed is False
    assert contract.next_round_work_item is None
    assert "comparator_unhealthy" in contract.hard_gate_failures
    assert contract.gate_evaluation.failure_category == "environment_failure"
    assert contract.gate_evaluation.responsibility_scope == "environment"
    assert contract.gate_evaluation.score_breakdown["agent_failure_counted"] == 0.0
    assert contract.repair_work_item is not None
    assert contract.repair_work_item.owner == "nuo"
    assert "without counting an agent failure" in contract.repair_work_item.expected_output


def test_kun_gate_failure_generates_qi_repair_work_item() -> None:
    contract = build_qi_ab_round_contract(
        _summary(
            kun_gate_passed=False,
            kun_result_quality=0.63,
            repair_ticket_refs=["artifact-ticket-round-01"],
        ),
        next_round_id="round-02",
        next_round_task_ids=["abfrontier-0006"],
    )

    assert contract.verdict == "repair"
    assert contract.round_valid is True
    assert contract.agent_failure_counted is True
    assert contract.next_round_allowed is False
    assert contract.next_round_work_item is None
    assert contract.work_item_result.status == "failed"
    assert contract.work_item_result.failure_category == "model_quality_failure"
    assert contract.gate_evaluation.next_action == "needs_repair"
    assert contract.gate_evaluation.next_state == "repairing"
    assert contract.gate_evaluation.learning_eligibility == "candidate"
    assert contract.gate_evaluation.score_breakdown["repair_ticket_count"] == 1.0
    assert contract.repair_work_item is not None
    assert contract.repair_work_item.owner == "qi"
    assert contract.repair_work_item.type == "repair"
    assert contract.repair_work_item.dependencies == ["work-qi-ab-round-01"]
    assert "same-task replay" in contract.repair_work_item.expected_output


def test_missing_repair_tickets_is_a_contract_gate_failure_not_next_round() -> None:
    contract = build_qi_ab_round_contract(
        _summary(kun_gate_passed=False, kun_result_quality=0.59),
        next_round_id="round-02",
        next_round_task_ids=["abfrontier-0006"],
    )

    assert contract.verdict == "invalid"
    assert contract.round_valid is False
    assert contract.agent_failure_counted is False
    assert contract.next_round_work_item is None
    assert "repair_tickets_missing" in contract.hard_gate_failures
    assert contract.gate_evaluation.failure_category == "tool_failure"
    assert contract.repair_work_item is not None
    assert "Produce missing KUN repair tickets" in contract.repair_work_item.expected_output


def test_answer_review_report_and_health_gates_are_hard_requirements() -> None:
    contract = build_qi_ab_round_contract(
        _summary(
            answer_refs=_refs("answer", 19),
            review_refs=_refs("review", 44),
            report_ref=None,
            health_ref=None,
        )
    )

    assert contract.verdict == "invalid"
    assert contract.round_valid is False
    assert contract.agent_failure_counted is False
    assert {
        "answer_count_below_threshold",
        "review_count_below_threshold",
        "report_missing",
        "health_missing",
    }.issubset(set(contract.hard_gate_failures))
    assert contract.gate_evaluation.score_breakdown["answer_count"] == 19.0
    assert contract.gate_evaluation.score_breakdown["review_count"] == 44.0
    assert contract.gate_evaluation.score_breakdown["report_present"] == 0.0
    assert contract.gate_evaluation.score_breakdown["health_present"] == 0.0
