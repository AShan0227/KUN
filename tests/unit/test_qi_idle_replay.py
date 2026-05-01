from __future__ import annotations

import sys

import pytest
from kun.qi.idle_replay import (
    HEURISTIC_IDLE_REPLAY_ENGINE,
    CommandLocalReplayModelEvaluator,
    IdleReplayGenerator,
    ReplayEvaluationBudget,
    TaskHistorySummary,
    evaluate_idle_replay_pool,
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
    assert signal.evidence["strategy_pack_draft"]["production_action"] is False
    assert signal.evidence["strategy_pack_draft"]["requires_human_review"] is True


def test_idle_replay_strategy_pack_draft_is_review_only() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="critical",
        task_type="world.email",
        summary="Email handler needs safer idempotency and compensation coverage",
        source="nuo.system_health",
        evidence={"risk": "critical"},
    )

    candidate = IdleReplayGenerator().generate_from_signal(signal)
    draft = candidate.to_strategy_pack_draft()

    assert draft.draft_id.startswith("spd_")
    assert draft.candidate_id == candidate.candidate_id
    assert draft.proposed_pack_id.startswith("qi_world_")
    assert draft.status == "needs_strong_review"
    assert draft.requires_human_review is True
    assert draft.requires_strong_review is True
    assert draft.production_action is False
    assert draft.default_execution_mode == "MAX"
    assert "world*" in draft.task_type_patterns
    assert "approval_drafter" in draft.skill_hints
    assert "unauthorized_side_effect" in draft.risk_watch
    assert "strong_model_review_passed" in draft.promotion_conditions
    assert draft.evidence["production_action"] is False


def test_idle_replay_low_risk_strategy_pack_draft_still_needs_human_review() -> None:
    history = TaskHistorySummary(
        history_id="hist-ok",
        task_type="marketing.ad",
        summary="Successful ad task reused a strong hook structure",
        outcome="completed",
        risk="low",
        verification_status="passed",
    )

    candidate = IdleReplayGenerator().generate_from_history(history)
    draft = candidate.to_strategy_pack_draft()

    assert draft.status == "draft"
    assert draft.requires_human_review is True
    assert draft.requires_strong_review is False
    assert draft.production_action is False
    assert "marketing*" in draft.task_type_patterns
    assert "conversion_reviewer" in draft.skill_hints
    assert "human_review_approved" in draft.promotion_conditions


@pytest.mark.asyncio
async def test_replay_evaluation_pool_scores_candidates_review_only() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="delivery",
        severity="warning",
        task_type="delivery",
        summary="Delivery missed artifact verification",
        source="nuo.system_health",
    )
    candidate = IdleReplayGenerator().generate_from_signal(signal)

    result = await evaluate_idle_replay_pool([candidate])

    assert result.evaluated == 1
    assert result.promotion_allowed is False
    record = result.records[0]
    assert record.status == "evaluated"
    assert 0.0 <= record.score <= 1.0
    assert record.cost_estimate_usd > 0
    assert record.promotion_allowed is False
    assert record.production_action is False
    assert record.evidence["review_only"] is True


@pytest.mark.asyncio
async def test_replay_evaluation_pool_budget_exhaustion_blocks_evaluation() -> None:
    candidates = generate_idle_replay_candidates(
        [
            {
                "history_id": "hist-budget",
                "task_type": "coding",
                "summary": "Successful task with reusable regression checks",
                "outcome": "completed",
            }
        ]
    )

    result = await evaluate_idle_replay_pool(
        candidates,
        budget=ReplayEvaluationBudget(max_items=1, max_cost_usd=0.0, max_concurrency=1),
    )

    assert result.evaluated == 0
    assert result.skipped_budget_exhausted == 1
    assert result.records[0].status == "skipped_budget_exhausted"
    assert result.records[0].promotion_allowed is False


@pytest.mark.asyncio
async def test_replay_evaluation_pool_high_risk_requires_strong_review() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="critical",
        task_type="world.email",
        summary="Outbound email handler retried without idempotency",
        source="nuo.system_health",
    )
    draft = IdleReplayGenerator().generate_from_signal(signal).to_strategy_pack_draft()

    result = await evaluate_idle_replay_pool([draft])

    record = result.records[0]
    assert record.status == "evaluated"
    assert record.risk == "critical"
    assert record.requires_strong_review is True
    assert record.promotion_allowed is False


@pytest.mark.asyncio
async def test_replay_evaluation_pool_local_model_is_honestly_unavailable() -> None:
    candidate = generate_idle_replay_candidates(
        [
            {
                "history_id": "hist-model",
                "task_type": "general",
                "summary": "Candidate needs optional model review",
                "outcome": "completed",
            }
        ]
    )[0]

    result = await evaluate_idle_replay_pool([candidate], evaluator_kind="local_model")

    assert result.evaluated == 0
    assert result.unavailable == 1
    assert result.records[0].status == "unavailable"
    assert "no_model_score_claimed" in result.records[0].notes
    assert result.records[0].promotion_allowed is False


@pytest.mark.asyncio
async def test_replay_evaluation_pool_runs_opt_in_command_local_model() -> None:
    candidate = generate_idle_replay_candidates(
        [
            {
                "history_id": "hist-command-model",
                "task_type": "coding",
                "summary": "Completed task with reusable tests and rollback guardrail",
                "outcome": "completed",
            }
        ]
    )[0]
    model_script = (
        "import json, sys; "
        "payload=json.load(sys.stdin); "
        "assert payload['contract']['promotion_allowed'] is False; "
        "print(json.dumps({"
        "'score': 0.82, "
        "'notes': ['cheap_local_vote'], "
        "'risk': 'medium', "
        "'evidence': {'model': 'tiny-local-test'}"
        "}))"
    )
    evaluator = CommandLocalReplayModelEvaluator(
        [sys.executable, "-c", model_script],
        timeout_sec=5,
    )

    result = await evaluate_idle_replay_pool(
        [candidate],
        evaluator_kind="local_model",
        local_model_evaluator=evaluator,
        budget=ReplayEvaluationBudget(max_items=1, max_cost_usd=1.0, max_concurrency=1),
    )

    assert result.evaluated == 1
    record = result.records[0]
    assert record.status == "evaluated"
    assert record.evaluator_kind == "local_model"
    assert record.score == 0.82
    assert record.evidence["model"] == "tiny-local-test"
    assert "cheap_local_vote" in record.notes
    assert record.promotion_allowed is False
    assert record.production_action is False
