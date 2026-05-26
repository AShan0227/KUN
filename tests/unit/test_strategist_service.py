"""L2.7 — StrategistService 单测 (第一条 RSI 实例 + 自指限制)."""

from __future__ import annotations

import pytest
from kun.agents.strategist.service import (
    StrategistService,
    StrategyExperiment,
    _is_self_referential,
    experiment_as_dict,
)

# ---- 自指检测 ----


def test_self_referential_matches_role_prefixes() -> None:
    assert _is_self_referential("strategist")
    assert _is_self_referential("strategist.service")
    assert _is_self_referential("kun.agents.strategist")
    assert _is_self_referential("kun/agents/supervisor/service")
    assert _is_self_referential("director.intent")
    assert _is_self_referential("gate.capability_writeback")
    assert _is_self_referential("external_supervisor.modes")


def test_self_referential_rejects_other_modules() -> None:
    assert _is_self_referential("executor.tool_calling") is False
    assert _is_self_referential("llm.router") is False
    assert _is_self_referential("kun/interface/llm/router") is False
    assert _is_self_referential("watchtower") is False


# ---- llm_fallback_spike candidates ----


@pytest.mark.asyncio
async def test_llm_fallback_spike_yields_three_explorer_modes() -> None:
    svc = StrategistService()
    request = {
        "request_id": "ss-1",
        "tenant_id": "u-test",
        "anomaly_kind": "llm_fallback_spike",
        "target_module": "llm.router",
        "evidence": [
            {
                "type": "fallback_count",
                "count": 5,
                "primary_provider": "anthropic",
                "primary_model": "claude-opus-4-7",
                "fallback_provider": "openai",
            }
        ],
    }
    candidates = await svc.propose_candidates(request)
    assert len(candidates) == 3
    modes = {c.explorer_mode for c in candidates}
    assert modes == {"conservative", "aggressive", "performance"}

    # conservative: tier upgrade
    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    assert conservative.change_spec["kind"] == "tier_upgrade"
    assert conservative.rollout_mode == "canary"
    assert conservative.sampling_rate == 0.3

    # aggressive: primary swap to fallback provider
    aggressive = next(c for c in candidates if c.explorer_mode == "aggressive")
    assert aggressive.change_spec["kind"] == "primary_swap"
    assert aggressive.change_spec["new_primary"] == "openai"

    # performance: retry budget increase
    performance = next(c for c in candidates if c.explorer_mode == "performance")
    assert performance.change_spec["kind"] == "retry_budget_increase"
    assert performance.rollout_mode == "shadow"


@pytest.mark.asyncio
async def test_llm_fallback_spike_without_fallback_provider_skips_aggressive() -> None:
    """aggressive 候选依赖 fallback_provider; evidence 没有就跳过."""
    svc = StrategistService()
    request = {
        "anomaly_kind": "llm_fallback_spike",
        "target_module": "llm.router",
        "evidence": [{"primary_provider": "anthropic"}],
    }
    candidates = await svc.propose_candidates(request)
    modes = {c.explorer_mode for c in candidates}
    assert "aggressive" not in modes
    assert "conservative" in modes
    assert "performance" in modes


@pytest.mark.asyncio
async def test_llm_fallback_spike_acceptance_thresholds_set() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [],
        }
    )
    for c in candidates:
        assert c.acceptance_threshold > 0
        assert c.success_metric != ""
        assert c.rollback_on  # 必须有 rollback 触发器
        assert c.ttl_seconds > 0


# ---- task_failure_spike ----


@pytest.mark.asyncio
async def test_task_failure_spike_yields_conservative() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "executor.coding.refactor",
            "evidence": [{"task_type": "coding.refactor", "count": 3}],
        }
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.explorer_mode == "conservative"
    assert c.change_spec["kind"] == "tier_upgrade"
    assert c.change_spec["task_types"] == ["coding.refactor"]
    assert c.target_level == 2


# ---- 自指限制实际生效 ----


@pytest.mark.asyncio
async def test_self_referential_target_marked_for_human_review() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "strategist.service",
            "evidence": [{"task_type": "x"}],
        }
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.requires_human_review is True
    assert c.status == "awaiting_human_review"
    assert "SELF-REFERENTIAL" in c.rationale


@pytest.mark.asyncio
async def test_non_self_referential_not_marked() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "executor.coding",
            "evidence": [{"task_type": "x"}],
        }
    )
    c = candidates[0]
    assert c.requires_human_review is False
    assert c.status == "pending"


# ---- unknown anomaly kind ----


@pytest.mark.asyncio
async def test_unknown_anomaly_kind_yields_empty() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {"anomaly_kind": "wild_anomaly", "target_module": "x", "evidence": []}
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_missing_anomaly_kind_yields_empty() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates({"target_module": "x"})
    assert candidates == []


# ---- emitter ----


@pytest.mark.asyncio
async def test_emitter_called_for_each_candidate() -> None:
    received: list[StrategyExperiment] = []

    async def fake_emitter(exp: StrategyExperiment) -> None:
        received.append(exp)

    svc = StrategistService(emitter=fake_emitter)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "minimax"}],
        }
    )
    assert len(received) == len(candidates)
    assert {c.experiment_id for c in candidates} == {r.experiment_id for r in received}


@pytest.mark.asyncio
async def test_emitter_exception_does_not_propagate() -> None:
    async def bad_emitter(exp: StrategyExperiment) -> None:
        raise RuntimeError("DB down")

    svc = StrategistService(emitter=bad_emitter)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "executor.x",
            "evidence": [],
        }
    )
    assert len(candidates) == 1  # 仍返回, emitter 错误吞


# ---- StrategyExperiment helpers ----


def test_to_row_payload_shape() -> None:
    exp = StrategyExperiment(
        experiment_id="er-1",
        target_module="llm.router",
        target_level=1,
        change_spec={"kind": "test"},
        rollout_mode="canary",
        sampling_rate=0.1,
        success_metric="m",
        acceptance_threshold=0.9,
    )
    row = exp.to_row_payload(tenant_id="u-test")
    assert row["tenant_id"] == "u-test"
    assert row["experiment_id"] == "er-1"
    assert row["target_module"] == "llm.router"
    assert row["acceptance_threshold"] == 0.9
    assert row["status"] == "pending"


def test_experiment_as_dict_round_trip() -> None:
    exp = StrategyExperiment(
        experiment_id="er-1",
        target_module="x",
        target_level=1,
        change_spec={"k": "v"},
        rollout_mode="shadow",
        sampling_rate=1.0,
        success_metric="m",
        acceptance_threshold=0.5,
    )
    d = experiment_as_dict(exp)
    assert d["experiment_id"] == "er-1"
    assert d["change_spec"] == {"k": "v"}
