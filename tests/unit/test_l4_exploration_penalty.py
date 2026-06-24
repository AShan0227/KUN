"""L4.6 — Exploration Penalty 单测."""

from __future__ import annotations

import asyncio

import pytest
from kun.agents.strategist.service import StrategistService, StrategyExperiment
from kun.governance.exploration_penalty import (
    DEFAULT_MAX_RETRIES_PER_WINDOW,
    DEFAULT_PENALTY_WINDOW_SECONDS,
    ExplorationPenalty,
    PenaltyCheckResult,
    candidate_signature_key,
)


def _make_candidate(
    *,
    target_module: str = "llm.router",
    kind: str = "tier_upgrade",
    explorer_mode: str = "conservative",
) -> StrategyExperiment:
    return StrategyExperiment(
        experiment_id=f"er-{kind}-{explorer_mode}",
        target_module=target_module,
        target_level=1,
        change_spec={"kind": kind, "from_tier": "strong", "to_tier": "top"},
        rollout_mode="canary",
        sampling_rate=0.3,
        success_metric="x",
        acceptance_threshold=0.9,
        explorer_mode=explorer_mode,
    )


# ---- signature key ----


def test_signature_key_stable() -> None:
    a = _make_candidate()
    b = _make_candidate()
    assert candidate_signature_key(a) == candidate_signature_key(b)


def test_signature_key_differs_by_kind() -> None:
    a = _make_candidate(kind="tier_upgrade")
    b = _make_candidate(kind="primary_swap")
    assert candidate_signature_key(a) != candidate_signature_key(b)


def test_signature_key_differs_by_target() -> None:
    a = _make_candidate(target_module="llm.router")
    b = _make_candidate(target_module="llm.context")
    assert candidate_signature_key(a) != candidate_signature_key(b)


def test_signature_key_does_not_dump_full_dict() -> None:
    """signature 应是 string 而非整 dict, 长度有限."""
    c = _make_candidate()
    sig = candidate_signature_key(c)
    assert isinstance(sig, str)
    # 不应直接含 change_spec dict literal
    assert "{" not in sig


# ---- ExplorationPenalty basics ----


def test_defaults_reasonable() -> None:
    p = ExplorationPenalty()
    assert p._max_retries == DEFAULT_MAX_RETRIES_PER_WINDOW
    assert p._window_seconds == DEFAULT_PENALTY_WINDOW_SECONDS


def test_invalid_args_rejected() -> None:
    with pytest.raises(ValueError):
        ExplorationPenalty(max_retries_per_window=0)
    with pytest.raises(ValueError):
        ExplorationPenalty(window_seconds=0)


@pytest.mark.asyncio
async def test_fresh_candidate_not_blocked() -> None:
    p = ExplorationPenalty()
    c = _make_candidate()
    result = await p.check("u-test", c)
    assert isinstance(result, PenaltyCheckResult)
    assert result.blocked is False
    assert result.current_failure_count == 0


@pytest.mark.asyncio
async def test_blocks_after_max_retries() -> None:
    p = ExplorationPenalty(max_retries_per_window=3)
    c = _make_candidate()
    for _ in range(3):
        await p.record_failure("u-test", c, reason="rollback")
    result = await p.check("u-test", c)
    assert result.blocked is True
    assert result.current_failure_count == 3
    assert "3 times" in result.reason


@pytest.mark.asyncio
async def test_record_success_clears_failures() -> None:
    p = ExplorationPenalty(max_retries_per_window=3)
    c = _make_candidate()
    for _ in range(3):
        await p.record_failure("u-test", c, reason="x")
    # 此时 blocked
    assert (await p.check("u-test", c)).blocked is True
    # 一旦成功 → 清
    await p.record_success("u-test", c)
    result = await p.check("u-test", c)
    assert result.blocked is False
    assert result.current_failure_count == 0


@pytest.mark.asyncio
async def test_different_signatures_isolated() -> None:
    p = ExplorationPenalty(max_retries_per_window=2)
    a = _make_candidate(kind="tier_upgrade")
    b = _make_candidate(kind="primary_swap")
    # a 失败 2 次 (blocked); b 应未受影响
    for _ in range(2):
        await p.record_failure("u-test", a)
    assert (await p.check("u-test", a)).blocked is True
    assert (await p.check("u-test", b)).blocked is False


@pytest.mark.asyncio
async def test_tenant_isolated() -> None:
    p = ExplorationPenalty(max_retries_per_window=2)
    c = _make_candidate()
    for _ in range(2):
        await p.record_failure("u-a", c)
    # u-a blocked, u-b 不受影响
    assert (await p.check("u-a", c)).blocked is True
    assert (await p.check("u-b", c)).blocked is False


@pytest.mark.asyncio
async def test_window_purge_resets_counts() -> None:
    p = ExplorationPenalty(max_retries_per_window=2, window_seconds=1)
    c = _make_candidate()
    for _ in range(2):
        await p.record_failure("u-test", c)
    assert (await p.check("u-test", c)).blocked is True
    # 等过窗口
    await asyncio.sleep(1.1)
    result = await p.check("u-test", c)
    assert result.blocked is False
    assert result.current_failure_count == 0


@pytest.mark.asyncio
async def test_filter_candidates_drops_blocked() -> None:
    p = ExplorationPenalty(max_retries_per_window=2)
    a = _make_candidate(kind="tier_upgrade")
    b = _make_candidate(kind="primary_swap")
    # a 失败 2 次 → blocked
    for _ in range(2):
        await p.record_failure("u-test", a)
    filtered = await p.filter_candidates("u-test", [a, b])
    # a 被剔, b 通过
    assert len(filtered) == 1
    assert filtered[0].change_spec["kind"] == "primary_swap"


@pytest.mark.asyncio
async def test_snapshot_lists_tracked_signatures() -> None:
    p = ExplorationPenalty(max_retries_per_window=3)
    c = _make_candidate()
    await p.record_failure("u-test", c)
    snap = await p.snapshot("u-test")
    sig = candidate_signature_key(c)
    assert sig in snap["tracked_signatures"]
    assert snap["failure_counts"][sig] == 1


# ---- StrategistService 集成 ----


@pytest.mark.asyncio
async def test_strategist_filters_via_penalty() -> None:
    """显式 record_failure 后, 同 signature 的 candidate 不再 emit."""
    penalty = ExplorationPenalty(max_retries_per_window=2)
    svc = StrategistService(exploration_penalty=penalty)
    # 第 1 次 propose — 全 3 个 candidate 都通过
    first = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(first) == 3
    # 标 conservative candidate 失败 2 次
    cons = next(c for c in first if c.explorer_mode == "conservative")
    for _ in range(2):
        await penalty.record_failure("default", cons)
    # 第 2 次 propose — conservative 被惩罚 filter 掉
    second = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    modes = {c.explorer_mode for c in second}
    assert "conservative" not in modes
    assert "aggressive" in modes  # 其他 mode 不受影响


@pytest.mark.asyncio
async def test_strategist_no_penalty_unchanged() -> None:
    """无 penalty 注入时行为不变."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 3


@pytest.mark.asyncio
async def test_strategist_penalty_clears_on_success() -> None:
    penalty = ExplorationPenalty(max_retries_per_window=2)
    svc = StrategistService(exploration_penalty=penalty)
    # 拿到 conservative candidate, mark 2 failures
    first = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    cons = next(c for c in first if c.explorer_mode == "conservative")
    for _ in range(2):
        await penalty.record_failure("default", cons)
    # 中间一次 success → clear
    await penalty.record_success("default", cons)
    # 再 propose — conservative 重新可用
    third = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    modes = {c.explorer_mode for c in third}
    assert "conservative" in modes
