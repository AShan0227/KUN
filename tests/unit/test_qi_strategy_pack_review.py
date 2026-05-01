from __future__ import annotations

from typing import Any

import pytest
from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.storage import InMemoryAssetStore
from kun.qi.idle_replay import IdleReplayGenerator, TaskHistorySummary
from kun.qi.strategy_pack_review import (
    review_strategy_pack_draft_asset,
    review_strategy_pack_draft_assets,
    summarize_strategy_pack_evidence,
)


def _strategy_asset(
    *,
    tenant_id: str = "t-1",
    task_type: str = "marketing.ad",
    risk: str = "low",
    requires_strong_review: bool = False,
    evaluation_records: list[dict[str, Any]] | None = None,
    lab_replay_records: list[dict[str, Any]] | None = None,
    tree_search_records: list[dict[str, Any]] | None = None,
) -> LayeredAsset:
    history = TaskHistorySummary(
        history_id="hist-1",
        task_type=task_type,
        summary="Historical task found a reusable strategy",
        outcome="completed",
        risk=risk,
    )
    candidate = IdleReplayGenerator().generate_from_history(history)
    draft = candidate.to_strategy_pack_draft().model_copy(
        update={
            "requires_strong_review": requires_strong_review,
            "status": "needs_strong_review" if requires_strong_review else "draft",
        }
    )
    payload = draft.model_dump(mode="json")
    payload.setdefault("evidence", {})["source_candidate"] = {
        "risk": risk,
        "task_type": task_type,
    }
    return LayeredAsset.build(
        "methodology",
        tenant_id,
        metadata={
            "source": "qi.idle_replay.strategy_pack_draft",
            "draft_id": draft.draft_id,
            "proposed_pack_id": draft.proposed_pack_id,
            "requires_human_review": True,
            "requires_strong_review": requires_strong_review,
            "production_action": False,
            "evaluation_records": evaluation_records or [],
            "lab_replay_records": lab_replay_records or [],
            "tree_search_records": tree_search_records or [],
            "strategy_pack_draft": payload,
        },
        summary="Qi review-only strategy draft",
        layer=AssetLayer.L2_PROJECT,
        tags=["qi", "strategy_pack_draft", "review_only"],
    )


def _eval_record(
    *,
    kind: str = "heuristic",
    score: float = 0.7,
    status: str = "evaluated",
    evaluation_id: str = "eval-1",
) -> dict[str, Any]:
    return {
        "evaluation_id": evaluation_id,
        "target_id": "draft-1",
        "target_kind": "strategy_pack_draft",
        "evaluator_kind": kind,
        "status": status,
        "score": score,
        "promotion_allowed": False,
        "production_action": False,
    }


def _lab_record(*, score: float = 0.7, status: str = "evaluated") -> dict[str, Any]:
    return {
        "draft_id": "draft-1",
        "history_id": "hist-1",
        "task_type": "world.email",
        "status": status,
        "score": score,
        "promotion_allowed": False,
        "production_action": False,
    }


def _tree_record(*, score: float = 0.76, status: str = "evaluated") -> dict[str, Any]:
    return {
        "evaluation_id": "qits-1",
        "target_id": "draft-1",
        "target_kind": "strategy_pack_draft",
        "evaluator_kind": "tree_search",
        "status": status,
        "score": score,
        "best_score": score,
        "promotion_allowed": False,
        "production_action": False,
        "notes": ["review_only_tree_search"],
    }


def test_low_risk_strategy_draft_can_be_ready_for_human_review() -> None:
    asset = _strategy_asset(evaluation_records=[_eval_record(score=0.74)])

    decision = review_strategy_pack_draft_asset(asset)

    assert decision.status == "ready_for_human_review"
    assert decision.production_action is False
    assert decision.promotion_allowed is False
    assert decision.missing_evidence == []
    assert "evidence_chain_sufficient_for_human_review" in decision.reasons


def test_high_risk_strategy_draft_needs_strong_and_lab_evidence() -> None:
    asset = _strategy_asset(
        task_type="world.email",
        risk="critical",
        requires_strong_review=True,
        evaluation_records=[_eval_record(score=0.8)],
    )

    decision = review_strategy_pack_draft_asset(asset)

    assert decision.status == "needs_evidence"
    assert "strong_model_review" in decision.missing_evidence
    assert "lab_replay_evidence" in decision.missing_evidence


def test_strategy_pack_evidence_summary_compacts_review_only_records() -> None:
    asset = _strategy_asset(
        evaluation_records=[
            _eval_record(kind="local_model", score=0.72, evaluation_id="local"),
        ],
    )

    summary = summarize_strategy_pack_evidence(asset)

    assert summary.status == "ready_for_human_review"
    assert summary.production_action is False
    assert summary.promotion_allowed is False
    assert summary.review_only is True
    assert summary.missing_evidence == []
    assert "review_gate_ready_for_human_review" in summary.why_worth_human_review
    assert "local_model_replay_evaluation_score:0.72" in summary.why_worth_human_review
    assert summary.evidence_sources == [
        {
            "source": "local_model_replay_evaluation",
            "status": "evaluated",
            "score": 0.72,
            "record_id": "local",
            "review_only": True,
            "notes": [],
        }
    ]
    assert "review_only_not_production_evidence" in summary.risks


def test_tree_search_evidence_can_satisfy_low_risk_base_review() -> None:
    asset = _strategy_asset(tree_search_records=[_tree_record(score=0.77)])

    decision = review_strategy_pack_draft_asset(asset)
    summary = summarize_strategy_pack_evidence(asset, decision)

    assert decision.status == "ready_for_human_review"
    assert decision.score == 0.77
    assert summary.evidence_sources[0]["source"] == "qi_tree_search_evidence"
    assert "qi_tree_search_evidence_score:0.77" in summary.why_worth_human_review


def test_strategy_pack_evidence_summary_names_gaps_and_high_impact_risks() -> None:
    asset = _strategy_asset(
        task_type="world.email",
        risk="critical",
        requires_strong_review=True,
        evaluation_records=[_eval_record(score=0.8)],
    )

    summary = summarize_strategy_pack_evidence(asset)

    assert summary.status == "needs_evidence"
    assert summary.missing_evidence == ["strong_model_review", "lab_replay_evidence"]
    assert "candidate_has_signal_but_evidence_gaps_remain" in summary.why_worth_human_review
    assert "missing_required_evidence" in summary.risks
    assert "high_or_external_impact_requires_extra_review" in summary.risks


def test_rejected_or_very_low_evidence_blocks_strategy_draft() -> None:
    asset = _strategy_asset(
        task_type="world.email",
        risk="critical",
        requires_strong_review=True,
        evaluation_records=[
            _eval_record(score=0.8, evaluation_id="base"),
            _eval_record(kind="strong_model", score=0.2, evaluation_id="strong"),
        ],
        lab_replay_records=[_lab_record(score=0.75)],
    )

    decision = review_strategy_pack_draft_asset(asset)

    assert decision.status == "blocked"
    assert "strong_model_blocking_low_score" in decision.reasons


@pytest.mark.asyncio
async def test_review_strategy_pack_draft_assets_can_apply_status_tags() -> None:
    store = InMemoryAssetStore()
    asset = _strategy_asset(evaluation_records=[_eval_record(score=0.78)])
    await store.put(asset)

    report = await review_strategy_pack_draft_assets(
        tenant_id="t-1",
        store=store,
        dry_run=False,
    )

    updated = await store.get(asset.asset_id, tenant_id="t-1")
    assert report.scanned == 1
    assert report.updated == 1
    assert report.ready_for_human_review == 1
    assert updated is not None
    assert updated.l1_metadata["qi_review_status"] == "ready_for_human_review"
    assert updated.l1_metadata["qi_evidence_summary"]["status"] == "ready_for_human_review"
    assert updated.l1_metadata["qi_evidence_summary"]["production_action"] is False
    assert "qi_ready_for_human_review" in updated.tags
    assert updated.l1_metadata["production_action"] is False
