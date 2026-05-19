from __future__ import annotations

import pytest
from kun.control_plane.nuo import (
    NuoObservation,
    build_nuo_pollution_sample_library,
    build_nuo_recovery_plan,
    diagnose_nuo_health,
)


def _observation(**overrides: object) -> NuoObservation:
    payload: dict[str, object] = {
        "mission_id": "msn-nuo",
        "task_plan_version": "v1",
        "subject_ref": "round-1",
        "task_type": "self_improvement",
        "output_text": "valid answer",
        "requested_model_family": "codex",
        "actual_model_family": "codex",
        "requested_model_tier": "top",
        "actual_model_tier": "top",
        "report_ref": "report-1",
        "review_count": 45,
        "expected_review_count": 45,
        "comparator_healthy": True,
    }
    payload.update(overrides)
    return NuoObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"output_text": "[stub echo] hello"}, "stub_echo"),
        ({"fallback_engaged": True, "fallback_reason": "primary quota"}, "fallback"),
        ({"actual_model_tier": "fallback"}, "fallback"),
        ({"actual_model_family": "claude"}, "family_routing_mismatch"),
        ({"timed_out": True}, "timeout"),
        ({"error_text": "deadline exceeded while waiting for runner"}, "timeout"),
        ({"error_text": "Unexpected EOF from upstream"}, "network_eof"),
        ({"error_text": "connection reset by peer"}, "network_blocked"),
        ({"error_text": "wrapper not found: codex-msg"}, "wrapper_missing"),
        (
            {"error_text": "tool schema mismatch: unexpected argument --task-family"},
            "wrapper_contract_mismatch",
        ),
        ({"auth_failure": True}, "auth_failure"),
        ({"error_text": "403 forbidden: permission denied"}, "permission_denied"),
        ({"report_ref": None}, "report_missing"),
        ({"review_count": None}, "review_count_missing"),
        ({"review_count": 44}, "review_count_insufficient"),
        (
            {"comparator_healthy": False, "comparator_health_reason": "judge quorum failed"},
            "comparator_unhealthy",
        ),
    ],
)
def test_nuo_detects_contract_pollution_and_health_blockers(
    overrides: dict[str, object],
    expected_code: str,
) -> None:
    report = diagnose_nuo_health(_observation(**overrides))

    assert report.status == "blocked"
    assert [finding.code for finding in report.findings] == [expected_code]
    assert report.valid_for_ranking is False
    assert report.valid_for_delivery is False
    assert report.counts_as_kun_failure is False
    assert report.findings[0].counts_as_kun_failure is False


def test_nuo_marks_clean_observation_healthy_and_gate_continues() -> None:
    report = diagnose_nuo_health(_observation())

    assert report.status == "healthy"
    assert report.findings == []
    assert report.valid_for_ranking is True
    assert report.valid_for_delivery is True

    gate = report.to_gate_evaluation()
    assert gate.north_star_verdict == "pass"
    assert gate.next_action == "continue"
    assert gate.next_state == "running"
    assert gate.responsibility_scope == "unknown"
    assert gate.failure_category is None
    assert gate.governance_signal == "nuo_health_clear"


def test_nuo_polluted_report_converts_to_repair_gate_without_kun_failure() -> None:
    report = diagnose_nuo_health(
        _observation(
            output_text="[stub echo] benchmark prompt",
            fallback_engaged=True,
            fallback_reason="no primary provider",
        )
    )

    recommendation = report.recovery_recommendation()
    assert recommendation is not None
    assert recommendation.next_action == "needs_repair"
    assert recommendation.next_state == "repairing"
    assert recommendation.counts_as_kun_failure is False

    gate = report.to_gate_evaluation()
    assert gate.north_star_verdict == "fail"
    assert gate.next_action == "needs_repair"
    assert gate.next_state == "repairing"
    assert gate.failure_category == "tool_failure"
    assert gate.responsibility_scope == "environment"
    assert gate.learning_eligibility == "blocked"
    assert set(gate.hard_gate_failures) == {"stub_echo", "fallback"}


def test_nuo_environment_blocker_routes_auth_to_human_without_kun_failure() -> None:
    report = diagnose_nuo_health(_observation(auth_failure=True))

    assert report.environment_blocked is True
    recommendation = report.recovery_recommendation()
    assert recommendation is not None
    assert recommendation.action == "fix_auth"
    assert recommendation.next_action == "needs_human"
    assert recommendation.next_state == "waiting_human"
    assert recommendation.failure_category == "permission_failure"

    gate = report.to_gate_evaluation()
    assert gate.failure_category == "permission_failure"
    assert gate.responsibility_scope == "environment"
    assert report.counts_as_kun_failure is False


def test_nuo_artifact_gaps_route_to_information_recovery() -> None:
    report = diagnose_nuo_health(_observation(report_ref=None, review_count=None))

    assert [finding.code for finding in report.findings] == [
        "report_missing",
        "review_count_missing",
    ]
    recommendation = report.recovery_recommendation()
    assert recommendation is not None
    assert recommendation.action == "collect_report"
    assert recommendation.next_action == "needs_info"
    assert recommendation.next_state == "info_gap"
    assert recommendation.failure_category == "evidence_failure"


def test_nuo_authorized_fallback_is_not_pollution() -> None:
    report = diagnose_nuo_health(
        _observation(
            fallback_engaged=True,
            fallback_authorized=True,
            actual_model_tier="fallback",
        )
    )

    assert report.status == "healthy"
    assert report.contamination_detected is False


def test_nuo_pollution_sample_library_classifies_real_failure_families() -> None:
    samples = build_nuo_pollution_sample_library()

    assert {
        "stub-echo-output",
        "unauthorized-fallback",
        "family-routing-mismatch",
        "runner-timeout",
        "network-eof",
        "network-blocked",
        "auth-failure",
        "permission-denied",
        "wrapper-missing",
        "wrapper-contract-change",
        "report-missing",
        "reviews-missing",
        "reviews-insufficient",
        "comparator-unhealthy",
    } == {sample.sample_id for sample in samples}

    for sample in samples:
        report = diagnose_nuo_health(sample.observation)
        recommendation = report.recovery_recommendation()

        assert report.status == "blocked", sample.sample_id
        assert [finding.code for finding in report.findings] == sample.expected_codes
        assert report.counts_as_kun_failure is sample.counts_as_kun_failure
        assert recommendation is not None
        assert recommendation.action == sample.expected_recovery_action


@pytest.mark.parametrize(
    ("overrides", "expected_type", "expected_owner", "expected_action"),
    [
        ({"error_text": "Unexpected EOF from upstream"}, "repair", "control-plane", "rerun"),
        (
            {"actual_model_family": "claude"},
            "repair",
            "control-plane",
            "reconfigure_router",
        ),
        (
            {"error_text": "tool schema mismatch: unexpected argument --run-tag"},
            "repair",
            "control-plane",
            "fix_wrapper",
        ),
        ({"auth_failure": True}, "collaboration", "operator", "fix_auth"),
        ({"report_ref": None}, "research", "qi", "collect_report"),
        ({"review_count": None}, "research", "qi", "collect_reviews"),
        (
            {"comparator_healthy": False, "comparator_health_reason": "judge quorum failed"},
            "repair",
            "control-plane",
            "repair_comparator",
        ),
    ],
)
def test_nuo_builds_recovery_work_item_for_pollution_and_system_blockers(
    overrides: dict[str, object],
    expected_type: str,
    expected_owner: str,
    expected_action: str,
) -> None:
    report = diagnose_nuo_health(_observation(**overrides))

    plan = build_nuo_recovery_plan(report)

    assert plan.gate_evaluation.governance_signal == "nuo_health_blocked"
    assert plan.recommendation is not None
    assert plan.recommendation.action == expected_action
    assert plan.recovery_work_item is not None
    assert plan.recovery_work_item.type == expected_type
    assert plan.recovery_work_item.owner == expected_owner
    assert plan.recovery_work_item.dependencies == [report.subject_ref]
    assert plan.recovery_work_item.idempotency_key is not None
    assert "Nuo findings:" in plan.recovery_work_item.expected_output


def test_nuo_recovery_plan_for_healthy_report_has_no_followup_work() -> None:
    report = diagnose_nuo_health(_observation())

    plan = build_nuo_recovery_plan(report)

    assert plan.gate_evaluation.north_star_verdict == "pass"
    assert plan.recommendation is None
    assert plan.recovery_work_item is None
