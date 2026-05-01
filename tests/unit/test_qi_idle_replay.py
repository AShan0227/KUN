from __future__ import annotations

from kun.qi.idle_replay import (
    HEURISTIC_IDLE_REPLAY_ENGINE,
    IdleReplayGenerator,
    TaskHistorySummary,
    generate_idle_replay_candidates,
)
from kun.qi.problem_queue import QiProblemSignal


def test_idle_replay_generates_honest_candidate_from_problem_signal() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="cost",
        severity="warning",
        task_type="coding.refactor",
        summary="LLM fallback cost spiked during retry loop",
        source="watchtower",
        evidence={"budget_breach": True, "cost_usd": 3.5},
    )

    candidate = IdleReplayGenerator().generate_from_signal(signal)

    assert candidate.engine == HEURISTIC_IDLE_REPLAY_ENGINE
    assert candidate.candidate_id.startswith("qir_")
    assert candidate.source_signal_id == signal.signal_id
    assert candidate.task_type == "coding.refactor"
    assert "bounded-step" in candidate.proposed_change
    assert candidate.risk == "high"
    assert candidate.requires_strong_review is True
    assert candidate.evidence["source_kind"] == "qi_problem_signal"
    assert candidate.evidence["engine"] == HEURISTIC_IDLE_REPLAY_ENGINE


def test_idle_replay_marks_critical_problem_for_strong_review() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="critical",
        task_type="world.email",
        summary="Outbound email handler retried without idempotency",
        source="nuo.system_health",
    )

    candidate = generate_idle_replay_candidates([signal])[0]

    assert candidate.risk == "critical"
    assert candidate.requires_strong_review is True
    assert "idempotency" in candidate.proposed_change
    assert candidate.to_lab_recipe_draft()["production_action"] is False


def test_idle_replay_accepts_lightweight_completed_task_history() -> None:
    history = TaskHistorySummary(
        history_id="task_123",
        task_type="delivery",
        summary="Delivered without matching artifact verification",
        outcome="completed_with_verification_failed",
        verification_status="failed",
        evidence={"changed_files": ["kun/foo.py"]},
    )

    candidate = IdleReplayGenerator().generate_from_history(history)

    assert candidate.source_signal_id == "task_123"
    assert candidate.risk == "high"
    assert candidate.requires_strong_review is True
    assert "reproduces the failure" in candidate.proposed_change
    assert candidate.evidence["source_kind"] == "task_history_summary"


def test_idle_replay_dict_input_and_signal_draft_are_review_only() -> None:
    candidates = generate_idle_replay_candidates(
        [
            {
                "task_type": "writing.plan",
                "summary": "Successful plan used explicit acceptance checks",
                "outcome": "completed",
                "risk": "low",
            }
        ]
    )

    candidate = candidates[0]
    signal = candidate.to_problem_signal(tenant_id="u-test")

    assert candidate.source_signal_id.startswith("history_")
    assert candidate.engine == HEURISTIC_IDLE_REPLAY_ENGINE
    assert signal.source == "qi.idle_replay.candidate"
    assert signal.evidence["production_action"] is False
    assert signal.evidence["candidate_id"] == candidate.candidate_id
