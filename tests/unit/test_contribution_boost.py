"""V2.2 Wire 15 — ContributionTracker + score_with_contribution_boost 集成测试.

验证 §25.3.4 注意力 = 信用分配 = 稀疏奖励 三件套联动.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import ImportanceScore, ImportanceScorer
from kun.engineering.credit_assignment import (
    ContributionTracker,
    CreditAssignment,
    TaskCreditReport,
    get_contribution_tracker,
    reset_contribution_tracker,
)


@pytest.fixture(autouse=True)
def _reset_tracker() -> None:
    reset_contribution_tracker()
    yield
    reset_contribution_tracker()


def _make_asset(asset_id: str, text: str = "") -> LayeredAsset:
    return LayeredAsset(
        asset_id=asset_id,
        asset_kind="memory",
        l1_metadata={"name": asset_id, "asset_id": asset_id},
        l2_summary=text,
        l3_ref=None,
        tags=[],
        access_count=1,
        last_accessed=datetime.now(UTC),
        tenant_id="t-test",
    )


# ---- ContributionTracker 算法 ----


def test_tracker_empty_returns_zero() -> None:
    t = ContributionTracker()
    assert t.contribution_score("m1", "memory") == 0.0


def test_tracker_pass_only_partial_score() -> None:
    """1 task pass, 但不是 critical → score=0.5 × 1.0 + 0.5 × 0 = 0.5."""
    t = ContributionTracker()
    ca = CreditAssignment()
    ca.record_step("tk-1", 1, {"memory": ["m1"]}, immediate_reward=0.5)
    import asyncio

    report: TaskCreditReport = asyncio.get_event_loop().run_until_complete(
        ca.finalize_task("tk-1", "pass", reflector=None)
    )
    t.update_from_report(report)
    score = t.contribution_score("m1", "memory")
    assert abs(score - 0.5) < 1e-9


@pytest.mark.asyncio
async def test_tracker_critical_path_full_score() -> None:
    """critical + pass → 0.5 + 0.5 = 1.0."""
    t = ContributionTracker()
    ca = CreditAssignment()
    ca.record_step("tk-2", 1, {"memory": ["m2"]}, immediate_reward=0.8)
    from kun.engineering.credit_assignment import heuristic_reflector

    report = await ca.finalize_task("tk-2", "pass", reflector=heuristic_reflector)
    # 单 step 自然 critical (heuristic 兜底)
    assert 1 in report.critical_path_step_ids
    t.update_from_report(report)
    score = t.contribution_score("m2", "memory")
    assert score == 1.0


@pytest.mark.asyncio
async def test_tracker_fail_task_zero_pass_share() -> None:
    """fail task → K=0 → 没 pass score 部分."""
    t = ContributionTracker()
    ca = CreditAssignment()
    ca.record_step("tk-3", 1, {"memory": ["m3"]}, immediate_reward=0.3)
    report = await ca.finalize_task("tk-3", "fail", reflector=None)
    t.update_from_report(report)
    score = t.contribution_score("m3", "memory")
    # K=0, M=0, N=1 → 0
    assert score == 0.0


@pytest.mark.asyncio
async def test_tracker_accumulates_across_tasks() -> None:
    """多 task 累计."""
    t = ContributionTracker()
    ca = CreditAssignment()
    for tid in range(1, 4):
        ca.record_step(f"tk-{tid}", 1, {"memory": ["m4"]}, immediate_reward=0.5)
        report = await ca.finalize_task(f"tk-{tid}", "pass", reflector=None)
        t.update_from_report(report)
    # N=3 K=3 M=0 → 0.5 × 1.0 + 0.5 × 0 = 0.5
    assert abs(t.contribution_score("m4", "memory") - 0.5) < 1e-9


# ---- score_with_contribution_boost ----


@pytest.mark.asyncio
async def test_no_lookup_no_boost() -> None:
    scorer = ImportanceScorer()
    candidates = [_make_asset("a"), _make_asset("b")]
    results = await scorer.score_with_contribution_boost(
        candidates,
        contribution_lookup=None,
        boost_weight=0.20,
    )
    assert len(results) == 2
    for _, score in results:
        assert score.contribution == 0.0


@pytest.mark.asyncio
async def test_high_contribution_boosts_overall() -> None:
    scorer = ImportanceScorer()
    candidates = [_make_asset("a-low"), _make_asset("b-high")]

    def lookup(asset_id: str) -> float:
        if asset_id == "b-high":
            return 0.9
        return 0.0

    results = await scorer.score_with_contribution_boost(
        candidates,
        contribution_lookup=lookup,
        boost_weight=0.30,
    )
    # b-high 应该被 boost +0.27 (0.30 × 0.9)
    score_map = {a.asset_id: s for a, s in results}
    assert score_map["b-high"].contribution == 0.9
    assert "contribution=0.90" in score_map["b-high"].rationale
    # b-high overall > a-low overall
    assert score_map["b-high"].overall > score_map["a-low"].overall


@pytest.mark.asyncio
async def test_contribution_boost_caps_at_one() -> None:
    scorer = ImportanceScorer()
    candidates = [_make_asset("hot")]
    results = await scorer.score_with_contribution_boost(
        candidates,
        contribution_lookup=lambda _: 1.0,
        boost_weight=0.99,
    )
    assert results[0][1].overall <= 1.0


@pytest.mark.asyncio
async def test_lookup_exception_falls_back_to_zero() -> None:
    """lookup 抛异常 → contribution=0, 不 crash."""
    scorer = ImportanceScorer()
    candidates = [_make_asset("a")]

    def bad_lookup(_):
        raise RuntimeError("db down")

    results = await scorer.score_with_contribution_boost(candidates, contribution_lookup=bad_lookup)
    assert results[0][1].contribution == 0.0


# ---- ImportanceScore.contribution 字段 ----


def test_importance_score_default_contribution_zero() -> None:
    s = ImportanceScore(overall=0.5, semantic=0.5, frequency=0.5, recency=0.5)
    assert s.contribution == 0.0


def test_importance_score_accepts_contribution() -> None:
    s = ImportanceScore(overall=0.5, semantic=0.5, frequency=0.5, recency=0.5, contribution=0.7)
    assert s.contribution == 0.7


# ---- 集成: 端到端 ----


@pytest.mark.asyncio
async def test_end_to_end_credit_to_attention() -> None:
    """完整链路: 任务 pass → 累计 contribution → 下次 score 自动 boost."""
    tracker = get_contribution_tracker()
    ca = CreditAssignment()

    # 跑 1 个任务, asset "winner" 在关键路径
    from kun.engineering.credit_assignment import heuristic_reflector

    ca.record_step("tk-1", 1, {"memory": ["winner"]}, immediate_reward=0.9)
    ca.record_step("tk-1", 2, {"memory": ["loser"]}, immediate_reward=0.0)
    report = await ca.finalize_task("tk-1", "pass", reflector=heuristic_reflector)
    tracker.update_from_report(report)

    # 现在 score: winner 应该 > loser
    scorer = ImportanceScorer()
    assets = [_make_asset("winner"), _make_asset("loser")]
    results = await scorer.score_with_contribution_boost(
        assets, contribution_lookup=lambda aid: tracker.contribution_score(aid, "memory")
    )
    score_map = {a.asset_id: s for a, s in results}
    assert score_map["winner"].contribution > score_map["loser"].contribution
