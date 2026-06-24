"""L4.4 — 合议层 dedup + cluster + rank 单测."""

from __future__ import annotations

import pytest
from kun.agents.strategist.deliberation import (
    DEFAULT_DEDUP_JACCARD_THRESHOLD,
    _candidate_signature,
    cluster_by_kind,
    deduplicate,
    deliberate,
    jaccard_similarity,
    rank_candidates,
)
from kun.agents.strategist.service import StrategistService, StrategyExperiment


def _make_candidate(
    *,
    target_module: str = "llm.router",
    target_level: int = 1,
    explorer_mode: str = "conservative",
    rollout_mode: str = "canary",
    sampling_rate: float = 0.3,
    kind: str = "tier_upgrade",
    extra_spec: dict | None = None,
    acceptance: float = 0.9,
    requires_human_review: bool = False,
) -> StrategyExperiment:
    spec = {"kind": kind}
    if extra_spec:
        spec.update(extra_spec)
    return StrategyExperiment(
        experiment_id=f"er-{kind}-{explorer_mode}",
        target_module=target_module,
        target_level=target_level,
        change_spec=spec,
        rollout_mode=rollout_mode,
        sampling_rate=sampling_rate,
        success_metric="rate",
        acceptance_threshold=acceptance,
        explorer_mode=explorer_mode,
        requires_human_review=requires_human_review,
    )


# ---- jaccard_similarity ----


def test_jaccard_identical_sets() -> None:
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint() -> None:
    assert jaccard_similarity({"a"}, {"b"}) == 0.0


def test_jaccard_partial() -> None:
    # 2/3 ≈ 0.667
    assert jaccard_similarity({"a", "b"}, {"a", "b", "c"}) == pytest.approx(2 / 3)


def test_jaccard_both_empty_is_one() -> None:
    assert jaccard_similarity(set(), set()) == 1.0


def test_jaccard_one_empty_is_zero() -> None:
    assert jaccard_similarity({"a"}, set()) == 0.0


# ---- _candidate_signature ----


def test_signature_contains_target_kind_mode() -> None:
    c = _make_candidate(
        target_module="llm.router",
        kind="tier_upgrade",
        explorer_mode="conservative",
    )
    sig = _candidate_signature(c)
    assert "target:llm.router" in sig
    assert "kind:tier_upgrade" in sig
    assert "mode:conservative" in sig
    assert "rollout:canary" in sig


def test_signature_different_target_yields_different_sig() -> None:
    a = _make_candidate(target_module="llm.router")
    b = _make_candidate(target_module="llm.context")
    sa = _candidate_signature(a)
    sb = _candidate_signature(b)
    assert sa != sb


# ---- deduplicate ----


def test_deduplicate_empty() -> None:
    assert deduplicate([]) == []


def test_deduplicate_single_unchanged() -> None:
    c = _make_candidate()
    out = deduplicate([c])
    assert out == [c]


def test_deduplicate_keeps_distinct_candidates() -> None:
    a = _make_candidate(kind="tier_upgrade", explorer_mode="conservative")
    b = _make_candidate(kind="primary_swap", explorer_mode="aggressive")
    c = _make_candidate(kind="retry_budget_increase", explorer_mode="performance")
    out = deduplicate([a, b, c])
    # 不同 kind, 不同 mode → 不合并
    assert len(out) == 3


def test_deduplicate_merges_identical_candidates() -> None:
    a = _make_candidate(acceptance=0.9)
    b = _make_candidate(acceptance=0.7)
    out = deduplicate([a, b])
    # 完全相同 → 合并; 保留 acceptance 更严 (=0.9) 的 a
    assert len(out) == 1
    assert out[0].acceptance_threshold == 0.9


def test_deduplicate_threshold_controls_aggressiveness() -> None:
    """高 threshold (1.0) 仅完全相同合并; 不同 mode 不合并."""
    a = _make_candidate(explorer_mode="conservative")
    b = _make_candidate(explorer_mode="aggressive")
    # 默认 0.7 — 两者除 mode 外字段都相同, 相似度高
    out_default = deduplicate([a, b])
    assert len(out_default) == 1
    # threshold=1.0 强求完全相同 → 不合并
    out_strict = deduplicate([a, b], threshold=1.0)
    assert len(out_strict) == 2


def test_deduplicate_prefers_conservative_when_tied() -> None:
    """同样 acceptance 时, conservative 优先于 aggressive."""
    a = _make_candidate(acceptance=0.8, explorer_mode="aggressive")
    b = _make_candidate(acceptance=0.8, explorer_mode="conservative")
    out = deduplicate([a, b])
    assert len(out) == 1
    assert out[0].explorer_mode == "conservative"


# ---- cluster_by_kind ----


def test_cluster_by_kind_groups_correctly() -> None:
    a = _make_candidate(kind="tier_upgrade")
    b = _make_candidate(kind="tier_upgrade", explorer_mode="aggressive")
    c = _make_candidate(kind="retry_budget_increase")
    clusters = cluster_by_kind([a, b, c])
    by_kind = {cl.change_kind: cl for cl in clusters}
    assert "tier_upgrade" in by_kind
    assert "retry_budget_increase" in by_kind
    assert len(by_kind["tier_upgrade"].candidates) == 2
    assert len(by_kind["retry_budget_increase"].candidates) == 1


def test_cluster_separates_by_target_module() -> None:
    a = _make_candidate(target_module="llm.router", kind="tier_upgrade")
    b = _make_candidate(target_module="llm.context", kind="tier_upgrade")
    clusters = cluster_by_kind([a, b])
    # 不同 target_module → 不同 cluster
    assert len(clusters) == 2


# ---- rank_candidates ----


def test_rank_backward_before_others() -> None:
    backward = _make_candidate(
        kind="capability_rollback", explorer_mode="backward", rollout_mode="direct"
    )
    aggressive = _make_candidate(explorer_mode="aggressive")
    conservative = _make_candidate(explorer_mode="conservative")
    ranked = rank_candidates([aggressive, conservative, backward])
    assert ranked[0].explorer_mode == "backward"


def test_rank_conservative_before_aggressive() -> None:
    conservative = _make_candidate(explorer_mode="conservative", kind="A")
    aggressive = _make_candidate(explorer_mode="aggressive", kind="B")
    ranked = rank_candidates([aggressive, conservative])
    assert ranked[0].explorer_mode == "conservative"
    assert ranked[1].explorer_mode == "aggressive"


def test_rank_auto_before_human_review() -> None:
    auto = _make_candidate(kind="A", requires_human_review=False)
    human = _make_candidate(kind="B", requires_human_review=True)
    ranked = rank_candidates([human, auto])
    assert ranked[0].requires_human_review is False


def test_rank_lower_sampling_first_when_same_mode() -> None:
    """同 mode 时, 风险低 (sampling 小) 优先."""
    high_s = _make_candidate(kind="X", sampling_rate=0.5)
    low_s = _make_candidate(kind="Y", sampling_rate=0.2)
    ranked = rank_candidates([high_s, low_s])
    assert ranked[0].sampling_rate == 0.2


# ---- deliberate (主入口) ----


def test_deliberate_empty_returns_empty() -> None:
    assert deliberate([]) == []


def test_deliberate_dedup_then_rank() -> None:
    """dup 候选先合并, 留下的按 rank 排."""
    backward = _make_candidate(
        kind="capability_rollback", explorer_mode="backward", rollout_mode="direct"
    )
    conservative = _make_candidate(explorer_mode="conservative", kind="A")
    conservative_dup = _make_candidate(
        explorer_mode="conservative", kind="A", acceptance=0.7
    )
    aggressive = _make_candidate(explorer_mode="aggressive", kind="B")
    out = deliberate([aggressive, conservative, conservative_dup, backward])
    # dup 已合并 (留 acceptance 0.9 那个)
    assert len(out) == 3
    # rank: backward → conservative → aggressive
    assert out[0].explorer_mode == "backward"
    assert out[1].explorer_mode == "conservative"
    assert out[2].explorer_mode == "aggressive"


# ---- 集成: StrategistService 应用 deliberate ----


@pytest.mark.asyncio
async def test_strategist_propose_orders_candidates_by_deliberate() -> None:
    """StrategistService propose 后, 候选应已按 deliberate 顺序排好 (backward 不出现这里 — 无 history reader)."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    # 顺序应为 conservative (lowest sampling 0.3) → aggressive (0.5) → performance (1.0)
    # 注: aggressive 0.5 + performance 1.0 都按 explorer_mode rank 比较
    # conservative=1 < performance=2 < aggressive=3
    modes_in_order = [c.explorer_mode for c in candidates]
    assert modes_in_order[0] == "conservative"
    # performance 在 aggressive 之前 (rank 2 < 3)
    assert modes_in_order.index("performance") < modes_in_order.index("aggressive")


def test_default_threshold_in_reasonable_range() -> None:
    """DEDUP_JACCARD_THRESHOLD 应在 [0.5, 0.9] 范围 — 既不过严也不过松."""
    assert 0.5 <= DEFAULT_DEDUP_JACCARD_THRESHOLD <= 0.9
