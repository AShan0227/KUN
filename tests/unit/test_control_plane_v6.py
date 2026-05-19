from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.control_plane import (
    AcceptanceReview,
    ArtifactManifest,
    CollaborationTicket,
    GateEvaluation,
    LedgerEvent,
    WorkingContext,
    WorkItem,
    assert_transition_allowed,
    default_recovery_for_failure,
    validate_workitem_dag,
)
from pydantic import ValidationError


def _gate(**overrides: object) -> GateEvaluation:
    payload: dict[str, object] = {
        "mission_id": "msn-1",
        "task_plan_version": "v1",
        "subject_ref": "manifest-1",
        "stage": "delivery",
        "task_type": "product_development",
        "rubric_version": "rubric-v1",
        "metric_pack_version": "north-star-v1",
        "north_star_verdict": "pass",
        "result_quality": 0.91,
        "speed": 0.7,
        "cost": 0.65,
        "risk": 0.2,
        "evidence_quality": 0.88,
        "collaboration_quality": 0.8,
        "evidence_refs": ["artifact-evidence"],
        "artifact_refs": ["artifact-result"],
        "test_refs": ["artifact-test"],
        "confidence": 0.86,
        "next_action": "ready_to_deliver",
        "next_state": "delivering",
        "created_by": "kun",
    }
    payload.update(overrides)
    return GateEvaluation.model_validate(payload)


def test_gate_evaluation_enforces_north_star_hard_gate() -> None:
    with pytest.raises(ValidationError, match="result_quality below threshold"):
        _gate(result_quality=0.5, speed=1.0, cost=1.0)


def test_gate_evaluation_requires_traceability_for_delivery() -> None:
    with pytest.raises(ValidationError, match="artifact and evidence/test/review refs"):
        _gate(artifact_refs=[], evidence_refs=[], test_refs=[], review_refs=[])


def test_gate_evaluation_action_must_match_next_state() -> None:
    with pytest.raises(ValidationError, match="next_state"):
        _gate(next_action="needs_repair", next_state="delivering")


def test_acceptance_review_rework_requires_followup() -> None:
    with pytest.raises(ValidationError, match="requires changes or followup"):
        AcceptanceReview(
            mission_id="msn-1",
            task_plan_version="v1",
            delivery_manifest_ref="manifest-1",
            gate_evaluation_ref="gate-1",
            reviewer="owner",
            decision="rework_required",
            satisfaction=0.4,
        )


def test_delivery_manifest_requires_evidence_or_tests() -> None:
    with pytest.raises(ValidationError, match="requires evidence, test, or review refs"):
        ArtifactManifest(
            mission_id="msn-1",
            kind="delivery",
            artifact_refs=["artifact-result"],
            primary_artifact_ref="artifact-result",
            created_by="kun",
            content_hash="hash",
            supports_delivery=True,
        )


def test_strong_ledger_event_schema_for_message_and_decision() -> None:
    with pytest.raises(ValidationError, match="message ledger event missing"):
        LedgerEvent(
            mission_id="msn-1",
            sequence=1,
            event_type="message",
            actor="kun",
            correlation_id="corr-1",
            subject_ref="ticket-1",
            idempotency_key="evt-1",
            payload={"sender": "kun"},
        )

    event = LedgerEvent(
        mission_id="msn-1",
        sequence=2,
        event_type="decision",
        actor="kun",
        correlation_id="corr-1",
        subject_ref="plan-1",
        idempotency_key="evt-2",
        payload={
            "options": ["continue", "ask"],
            "selected_option": "ask",
            "reason": "missing approval",
            "risk_impact": "lower",
            "quality_impact": "higher",
            "speed_impact": "slower",
            "cost_impact": "neutral",
            "approver": "owner",
        },
    )
    assert event.event_type == "decision"


def test_working_context_requires_core_constraints_and_invalidation_reason() -> None:
    with pytest.raises(ValidationError, match="acceptance criteria"):
        WorkingContext(
            mission_id="msn-1",
            task_plan_version="v1",
            audience="worker",
            scope="implementation",
            summary="Build the thing",
            constraints=["no external writes"],
        )

    with pytest.raises(ValidationError, match="invalidated_by"):
        WorkingContext(
            mission_id="msn-1",
            task_plan_version="v1",
            audience="worker",
            scope="implementation",
            summary="Build the thing",
            acceptance_criteria=["tests pass"],
            constraints=["no external writes"],
            freshness="invalidated",
        )


def test_collaboration_ticket_decision_needs_options_and_deadline() -> None:
    deadline = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(ValidationError, match="requires decision options"):
        CollaborationTicket(
            mission_id="msn-1",
            type="user_decision",
            role_needed="owner",
            why_needed="Need scope choice",
            context_ref="ctx-1",
            risk_if_skipped="May build wrong thing",
            deadline=deadline,
            output_contract="Choose scope",
        )


def test_state_machine_blocks_illegal_jumps_and_terminal_transitions() -> None:
    assert_transition_allowed("planning", "awaiting_approval")
    assert_transition_allowed("running", "info_gap")
    with pytest.raises(ValueError, match="not allowed"):
        assert_transition_allowed("planning", "running")
    with pytest.raises(ValueError, match="terminal"):
        assert_transition_allowed("closed", "running")


def test_failure_recovery_matrix_routes_to_required_state() -> None:
    assert default_recovery_for_failure("permission_failure").next_state == "waiting_human"
    assert default_recovery_for_failure("evidence_failure").next_action == "needs_info"
    assert default_recovery_for_failure("plan_failure").next_state == "changing_plan"


def test_workitem_dag_rejects_missing_dependencies_and_cycles() -> None:
    first = WorkItem(
        work_item_id="work-1",
        mission_id="msn-1",
        task_plan_version="v1",
        type="research",
        owner="kun",
    )
    second = WorkItem(
        work_item_id="work-2",
        mission_id="msn-1",
        task_plan_version="v1",
        type="execution",
        owner="kun",
        dependencies=["work-1"],
    )
    validate_workitem_dag([first, second])

    missing = second.model_copy(update={"dependencies": ["work-missing"]})
    with pytest.raises(ValueError, match="missing dependencies"):
        validate_workitem_dag([first, missing])

    cycle_a = first.model_copy(update={"dependencies": ["work-2"]})
    with pytest.raises(ValueError, match="cycle detected"):
        validate_workitem_dag([cycle_a, second])
