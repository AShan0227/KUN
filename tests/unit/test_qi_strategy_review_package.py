from __future__ import annotations

import pytest
from kun.qi.idle_replay import (
    IdleReplayGenerator,
    ReplayEvaluationRecord,
)
from kun.qi.lab_replay import QiLabReplayRecord
from kun.qi.problem_queue import QiProblemSignal
from kun.qi.replay_tree_search import QiReplayTreeSearchRecord
from kun.qi.strategy_review_package import build_qi_strategy_review_packages


def _high_risk_candidate_and_draft():
    signal = QiProblemSignal.build(
        tenant_id="t-1",
        category="world_gateway",
        severity="critical",
        task_type="world.email",
        summary="Email handler retried without idempotency or compensation",
        source="nuo.system_health",
    )
    candidate = IdleReplayGenerator().generate_from_signal(signal)
    return candidate, candidate.to_strategy_pack_draft()


def _low_risk_candidate_and_draft():
    signal = QiProblemSignal.build(
        tenant_id="t-1",
        category="delivery",
        severity="info",
        task_type="delivery",
        summary="Successful delivery used artifact evidence",
        source="nuo.system_health",
    )
    candidate = IdleReplayGenerator().generate_from_signal(signal)
    return candidate, candidate.to_strategy_pack_draft()


@pytest.mark.unit
def test_strategy_review_package_separates_local_exploration_from_strong_gate() -> None:
    candidate, draft = _high_risk_candidate_and_draft()
    heuristic = ReplayEvaluationRecord(
        evaluation_id="eval-1",
        target_id=draft.draft_id,
        target_kind="strategy_pack_draft",
        evaluator="heuristic",
        evaluator_kind="heuristic",
        status="evaluated",
        score=0.72,
        risk="critical",
        requires_strong_review=True,
    )
    tree = QiReplayTreeSearchRecord(
        target_id=draft.draft_id,
        target_kind="strategy_pack_draft",
        evaluation_id="tree-1",
        status="evaluated",
        score=0.81,
        best_score=0.81,
        total_cost_usd=0.004,
        nodes_evaluated=3,
    )

    packages = build_qi_strategy_review_packages(
        candidates=[candidate],
        drafts=[draft],
        evaluation_records=[heuristic],
        tree_search_records=[tree],
    )

    assert len(packages) == 1
    package = packages[0]
    assert package.review_only is True
    assert package.production_action is False
    assert package.promotion_allowed is False
    assert package.status == "needs_strong_review"
    assert package.recommendation == "queue_strong_review_before_human_rollout_review"
    assert package.strong_review_gate.required is True
    assert package.strong_review_gate.status == "missing"
    assert "strong_review_gate" in package.missing_evidence
    assert {item.channel for item in package.local_exploration} == {"heuristic", "tree_search"}
    assert package.best_local_score == 0.81


@pytest.mark.unit
def test_strategy_review_package_can_be_ready_only_for_human_review() -> None:
    candidate, draft = _low_risk_candidate_and_draft()
    heuristic = ReplayEvaluationRecord(
        evaluation_id="eval-1",
        target_id=draft.draft_id,
        target_kind="strategy_pack_draft",
        evaluator="heuristic",
        evaluator_kind="heuristic",
        status="evaluated",
        score=0.68,
        risk="low",
        requires_strong_review=False,
    )
    replay = QiLabReplayRecord(
        draft_id=draft.draft_id,
        history_id="hist-1",
        task_type="delivery",
        status="evaluated",
        score=0.74,
        experiment_id="lab-1",
        notes=["lab evidence"],
    )

    packages = build_qi_strategy_review_packages(
        candidates=[candidate],
        drafts=[draft],
        evaluation_records=[heuristic],
        lab_replay_records=[replay],
    )

    package = packages[0]
    assert package.status == "ready_for_human_review"
    assert package.recommendation == "ready_for_human_review_only"
    assert package.strong_review_gate.required is False
    assert package.strong_review_gate.status == "not_required"
    assert package.missing_evidence == []
    assert {item.channel for item in package.local_exploration} == {"heuristic", "lab_replay"}


@pytest.mark.unit
def test_strategy_review_package_rejects_weak_local_evidence() -> None:
    candidate, draft = _low_risk_candidate_and_draft()
    weak = ReplayEvaluationRecord(
        evaluation_id="eval-weak",
        target_id=draft.draft_id,
        target_kind="strategy_pack_draft",
        evaluator="heuristic",
        evaluator_kind="heuristic",
        status="evaluated",
        score=0.22,
        risk="low",
        requires_strong_review=False,
    )

    package = build_qi_strategy_review_packages(
        candidates=[candidate],
        drafts=[draft],
        evaluation_records=[weak],
    )[0]

    assert package.status == "reject"
    assert package.next_review_action.startswith("rewrite candidate")
    assert package.production_action is False
