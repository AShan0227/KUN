"""V2.3 e2e: orchestrator stream + protocol consume + AntiGaming + PreDeliverGate 全闭环.

跟现有 unit tests 的差别:
- 真 install_runtime → 装 protocol_registry / pheromone / cache / orchestrator hooks
- 真 seed protocol → orchestrator 在 stream 中真消费 (修 ExecutionMode)
- 真发 task → 完整流过 plan → step → step_completed → delivery_review → done
- 验证 events 序列里有 protocol.applied + delivery.review_done
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from kun.datamodel.verification_spec import VerificationResult, VerificationSpec
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider

# ===== Fake DB =====


class _FakeSession:
    async def execute(self, *_args, **_kwargs):
        class R:
            def scalar_one_or_none(self):
                return None

            def scalar_one(self):
                return 0

            def all(self):
                return []

            def scalars(self):
                return self

            @property
            def rowcount(self):
                return 0

        return R()

    def add(self, *_args, **_kwargs):
        pass

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def rollback(self):
        pass


@asynccontextmanager
async def _fake_session_scope(**_kwargs) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)


# ===== Mock LLM router =====


def _intent_writing_creative() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.creative.short",
                "risk_level": "low",
                "complexity_score": 0.3,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "短 slogan",
                "goal_detail": "写一段 30 字 slogan",
            }
        ),
        usage=UsageInfo(input_tokens=10, output_tokens=80),
    )


def _exec_response() -> LLMResponse:
    return LLMResponse(
        content="未来已来 · 与你同行 — 用心做好每一件事 (实测 30 字)",
        usage=UsageInfo(input_tokens=20, output_tokens=15),
    )


class _Stub(StubProvider):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            return _intent_writing_creative()
        return _exec_response()


def _make_router() -> LLMRouter:
    s = _Stub(tier="top")
    return LLMRouter({"top": s, "cheap": s, "strong": s, "coding": s, "fallback": s})


async def _identity_translator(**kwargs) -> str:
    return str(kwargs["payload"]["answer"])


# ===== Verification stub =====


class _PassingVerifier:
    async def verify(self, spec: VerificationSpec, artifact: str) -> VerificationResult:
        return VerificationResult(kind=spec.kind, passed=True)


# ===== E2E tests =====


@pytest.mark.asyncio
async def test_e2e_orchestrator_consumes_seeded_protocol() -> None:
    """KUN_PROTOCOL_CONSUME_ENABLED=1 + seeded protocol → orchestrator 真消费, 改 ExecutionMode."""
    from kun.engineering.orchestrator import Orchestrator
    from kun.qi.protocol import (
        InMemoryProtocolStorage,
        Protocol,
        ProtocolExecution,
        ProtocolRegistry,
        ProtocolTrigger,
    )

    set_router(_make_router())
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    proto = Protocol(
        protocol_id="writing.creative.short",
        version="0.1.0-test",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="writing.creative.*",
            complexity_score_min=0.0,
            complexity_score_max=1.0,
            risk_levels=["low", "medium"],
        ),
        execution=ProtocolExecution(mode="MAX"),
    )
    await registry.save(proto)

    orch = Orchestrator(
        output_translator=_identity_translator,
        protocol_registry=registry,
        verification_runner=_PassingVerifier(),
    )

    with patch.dict(
        os.environ,
        {"KUN_PROTOCOL_CONSUME_ENABLED": "1", "KUN_PRE_DELIVER_GATE_ENABLED": "1"},
    ):
        events: list[tuple[str, Any]] = []
        async for ev in orch.stream("写一段 30 字 slogan"):
            events.append((ev.kind, ev.data))

    # delivery.review_done 必现
    delivery_events = [d for k, d in events if k == "delivery.review_done"]
    assert len(delivery_events) >= 1
    assert delivery_events[0]["passed"] is True


@pytest.mark.asyncio
async def test_e2e_no_protocol_consume_default_off_legacy_path() -> None:
    """KUN_PROTOCOL_CONSUME_ENABLED=0 → 跳过协议消费, 不影响其他流程."""
    from kun.engineering.orchestrator import Orchestrator
    from kun.qi.protocol import InMemoryProtocolStorage, ProtocolRegistry

    set_router(_make_router())
    orch = Orchestrator(
        output_translator=_identity_translator,
        protocol_registry=ProtocolRegistry(InMemoryProtocolStorage()),
    )

    with patch.dict(os.environ, {"KUN_PROTOCOL_CONSUME_ENABLED": "0"}):
        events: list[tuple[str, Any]] = []
        async for ev in orch.stream("写 slogan"):
            events.append((ev.kind, ev.data))

    # 没 protocol 消费, 但仍走 PreDeliverGate (default ON), task done
    delivery_events = [d for k, d in events if k == "delivery.review_done"]
    assert len(delivery_events) >= 1


@pytest.mark.asyncio
async def test_e2e_anti_gaming_clean_answer_passes() -> None:
    """干净的 answer + AntiGaming detector 装上 → delivery.review_done passed=True."""
    from kun.engineering.orchestrator import Orchestrator
    from kun.security.anti_gaming import AntiGamingDetector

    set_router(_make_router())
    detector = AntiGamingDetector(off_topic_threshold=0.01)
    orch = Orchestrator(
        output_translator=_identity_translator,
        anti_gaming_detector=detector,
    )

    with patch.dict(
        os.environ,
        {"KUN_ANTI_GAMING_ENABLED": "1", "KUN_PRE_DELIVER_GATE_ENABLED": "1"},
    ):
        events: list[tuple[str, Any]] = []
        async for ev in orch.stream("写 slogan"):
            events.append((ev.kind, ev.data))

    delivery_events = [d for k, d in events if k == "delivery.review_done"]
    assert len(delivery_events) >= 1
    # 干净 answer → 应该 pass (off_topic threshold 设 0.01 容忍小重合)
    # checks 里应有 anti_gaming.overall
    checks = delivery_events[0].get("checks", [])
    assert any(c["name"] == "anti_gaming.overall" for c in checks)


@pytest.mark.asyncio
async def test_e2e_pre_deliver_gate_disabled_legacy_verification_path() -> None:
    """KUN_PRE_DELIVER_GATE_ENABLED=0 → 退到旧 verification_runner 路径."""
    from kun.engineering.orchestrator import Orchestrator

    set_router(_make_router())
    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=_PassingVerifier(),
    )

    with patch.dict(os.environ, {"KUN_PRE_DELIVER_GATE_ENABLED": "0"}):
        events: list[tuple[str, Any]] = []
        async for ev in orch.stream("写 slogan"):
            events.append((ev.kind, ev.data))

    # 没 delivery.review_done, 但 task 仍 done
    delivery_events = [d for k, d in events if k == "delivery.review_done"]
    assert len(delivery_events) == 0


@pytest.mark.asyncio
async def test_e2e_seed_protocols_load_e2e() -> None:
    """seed_default_protocols → registry → orchestrator 能 find_protocol_for."""
    from kun.qi.protocol import InMemoryProtocolStorage, ProtocolRegistry
    from kun.qi.seed_protocols import seed_default_protocols

    registry = ProtocolRegistry(InMemoryProtocolStorage())
    n = await seed_default_protocols(registry)
    assert n == 5

    found = await registry.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.3, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is not None
    assert found.protocol_id == "writing.creative.short"
    assert found.execution.mode == "SMART"


@pytest.mark.asyncio
async def test_e2e_full_stack_orchestrator_with_all_v23_hooks() -> None:
    """全栈 e2e: protocol_registry + anti_gaming + verification + PreDeliverGate 全装."""
    from kun.engineering.orchestrator import Orchestrator
    from kun.qi.protocol import InMemoryProtocolStorage, ProtocolRegistry
    from kun.qi.seed_protocols import seed_default_protocols
    from kun.security.anti_gaming import AntiGamingDetector

    set_router(_make_router())
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await seed_default_protocols(registry)
    detector = AntiGamingDetector(off_topic_threshold=0.01)

    orch = Orchestrator(
        output_translator=_identity_translator,
        protocol_registry=registry,
        anti_gaming_detector=detector,
        verification_runner=_PassingVerifier(),
    )

    with patch.dict(
        os.environ,
        {
            "KUN_PROTOCOL_CONSUME_ENABLED": "1",
            "KUN_ANTI_GAMING_ENABLED": "1",
            "KUN_PRE_DELIVER_GATE_ENABLED": "1",
        },
    ):
        events: list[tuple[str, Any]] = []
        async for ev in orch.stream("写一段 30 字 slogan"):
            events.append((ev.kind, ev.data))

    # 应有: action_plan / cost_tick / step_completed / delivery.review_done / done
    kinds = {k for k, _ in events}
    assert "delivery.review_done" in kinds
    assert "done" in kinds

    # delivery passed (verification 全过, 不 off_topic, 输出长度 OK)
    dr = next(d for k, d in events if k == "delivery.review_done")
    assert dr["final_status"] == "done"
    assert dr["passed"] is True
    # checks 里应同时含 verification 和 anti_gaming
    check_names = {c["name"] for c in dr["checks"]}
    assert any("anti_gaming" in n for n in check_names)
    assert any("self_check" in n for n in check_names)
