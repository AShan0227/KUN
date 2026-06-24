"""L4.3 — External Supervisor Pool 配置化单测."""

from __future__ import annotations

import json

import pytest
from kun.external_supervisor.pool import (
    ExternalSupervisorPool,
    ExternalSupervisorPoolConfig,
    ExternalSupervisorPoolEntry,
)
from kun.external_supervisor.service import ExternalSupervisorService
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.stub_provider import StubProvider


def _stub_returning(content: str) -> StubProvider:
    def builder(req):
        return LLMResponse(
            content=content,
            usage=UsageInfo(input_tokens=10, output_tokens=20),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    return StubProvider(model_id="stub-1", tier="cheap", latency_ms=0, builder=builder)


# ---- ExternalSupervisorPoolConfig ----


def test_default_config_three_modes() -> None:
    cfg = ExternalSupervisorPoolConfig()
    modes = set(cfg.entries.keys())
    assert modes == {"gate_review", "task_debrief", "self_aggrandizement"}


def test_default_entries_have_correct_budgets() -> None:
    cfg = ExternalSupervisorPoolConfig()
    # gate_review: 严肃, 低 temperature
    assert cfg.entries["gate_review"].temperature == 0.1
    # task_debrief: 允许发散 + 输出长
    assert cfg.entries["task_debrief"].temperature == 0.3
    assert cfg.entries["task_debrief"].max_tokens == 1024
    # self_aggrandizement: 高频, 多并发
    assert cfg.entries["self_aggrandizement"].max_concurrent == 3
    assert cfg.entries["self_aggrandizement"].max_tokens == 256


def test_has_mode_and_all_modes() -> None:
    cfg = ExternalSupervisorPoolConfig()
    assert cfg.has_mode("gate_review")
    assert cfg.has_mode("random") is False
    assert set(cfg.all_modes()) == {
        "gate_review",
        "task_debrief",
        "self_aggrandizement",
    }


def test_custom_entries() -> None:
    cfg = ExternalSupervisorPoolConfig(
        entries={
            "experimental": ExternalSupervisorPoolEntry(
                mode="experimental", max_concurrent=5, temperature=0.5
            )
        }
    )
    assert cfg.has_mode("experimental")
    assert cfg.entries["experimental"].max_concurrent == 5


# ---- ExternalSupervisorPool ----


def test_pool_lazy_init_instance_per_mode() -> None:
    pool = ExternalSupervisorPool(llm_provider=_stub_returning('{"verdict": "ok"}'))
    gate = pool.instance_for("gate_review")
    debrief = pool.instance_for("task_debrief")
    assert isinstance(gate, ExternalSupervisorService)
    assert isinstance(debrief, ExternalSupervisorService)
    assert gate is not debrief
    # 同 mode 复取应返回同 instance
    assert pool.instance_for("gate_review") is gate


def test_pool_unknown_mode_uses_default_entry() -> None:
    """未配置 mode → 创建默认 budget instance (容错优先)."""
    pool = ExternalSupervisorPool(llm_provider=_stub_returning('{"verdict": "ok"}'))
    svc = pool.instance_for("unknown_mode")
    assert isinstance(svc, ExternalSupervisorService)
    # 默认 entry max_concurrent=2
    assert svc._sem._value == 2


def test_pool_max_concurrent_matches_entry() -> None:
    pool = ExternalSupervisorPool(llm_provider=_stub_returning('{"verdict": "ok"}'))
    # self_aggrandizement 默认 max_concurrent=3
    svc = pool.instance_for("self_aggrandizement")
    assert svc._sem._value == 3


@pytest.mark.asyncio
async def test_pool_dispatch_routes_to_correct_instance() -> None:
    pool = ExternalSupervisorPool(
        llm_provider=_stub_returning(
            json.dumps({"verdict": "ok", "rationale": "all clear"})
        )
    )
    obs = await pool.dispatch(
        "gate_review",
        obs_kind="gate_review",
        observation_payload={"test_report": {"passed": 10}},
        anchor={"goal_statement": "x"},
    )
    assert obs.verdict == "ok"
    assert obs.rationale == "all clear"


@pytest.mark.asyncio
async def test_pool_provider_factory_used_per_mode() -> None:
    """如果注入 provider_factory, 每 mode 用不同 provider."""

    def factory(mode: str):
        return _stub_returning(json.dumps({"verdict": "ok", "rationale": mode}))

    pool = ExternalSupervisorPool(
        llm_provider=_stub_returning('{"verdict": "ok"}'),  # fallback
        provider_factory=factory,
    )
    obs_gate = await pool.dispatch(
        "gate_review",
        obs_kind="gate_review",
        observation_payload={},
    )
    obs_debrief = await pool.dispatch(
        "task_debrief",
        obs_kind="task_debrief",
        observation_payload={},
    )
    # rationale 通过 factory 注入的 provider 不同
    assert obs_gate.rationale == "gate_review"
    assert obs_debrief.rationale == "task_debrief"


@pytest.mark.asyncio
async def test_pool_provider_factory_failure_falls_back_to_shared() -> None:
    def bad_factory(mode: str):
        raise RuntimeError("factory blew up")

    fallback = _stub_returning('{"verdict": "ok", "rationale": "fallback"}')
    pool = ExternalSupervisorPool(
        llm_provider=fallback,
        provider_factory=bad_factory,
    )
    obs = await pool.dispatch(
        "gate_review", obs_kind="gate_review", observation_payload={}
    )
    # 走 fallback shared provider
    assert obs.rationale == "fallback"


def test_pool_all_modes_returns_config_modes() -> None:
    pool = ExternalSupervisorPool(llm_provider=_stub_returning('{"verdict": "ok"}'))
    modes = pool.all_modes()
    assert set(modes) == {"gate_review", "task_debrief", "self_aggrandizement"}
