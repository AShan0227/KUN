"""Wire 41: Predictive Coding hook (V2.3 §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider


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


async def _identity_translator(**kwargs) -> str:
    return str(kwargs["payload"]["answer"])


def _intent_response() -> LLMResponse:
    import json

    return LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.simple",
                "risk_level": "low",
                "complexity_score": 0.2,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "say hello",
            }
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )


def _exec_response() -> LLMResponse:
    return LLMResponse(content="hello", usage=UsageInfo(input_tokens=10, output_tokens=4))


class _StubRouter(StubProvider):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            return _intent_response()
        return _exec_response()


def _make_router() -> LLMRouter:
    stub = _StubRouter(tier="top")
    return LLMRouter({"top": stub, "cheap": stub, "coding": stub, "fallback": stub})


# ---- 没装 plugin → 鲲行为完全不变 ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_pc_provider_no_pc_events() -> None:
    """没装 prediction_provider → 不 emit pc_expected/pc_error event."""
    set_router(_make_router())
    orch = Orchestrator(output_translator=_identity_translator)
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("hello"):
        events.append((ev.kind, ev.data))

    assert not any(k == "pc_expected" for k, _ in events)
    assert not any(k == "pc_error" for k, _ in events)


# ---- 装 prediction_provider → emit pc_expected ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pc_provider_emits_expected_event() -> None:
    set_router(_make_router())

    class _StubProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def predict(self, state: dict[str, Any]) -> dict[str, float]:
            self.calls.append(state)
            return {"cost_usd": 0.05, "duration_sec": 10.0, "tokens": 50}

    provider = _StubProvider()
    orch = Orchestrator(
        output_translator=_identity_translator,
        prediction_provider=provider,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("hello"):
        events.append((ev.kind, ev.data))

    pc_expected = [data for kind, data in events if kind == "pc_expected"]
    assert len(pc_expected) >= 1
    assert pc_expected[0]["expected"] == {"cost_usd": 0.05, "duration_sec": 10.0, "tokens": 50}
    # provider 真被调
    assert len(provider.calls) >= 1
    assert provider.calls[0]["task_type"] == "writing.simple"


# ---- 装 model_updater → emit pc_error ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pc_updater_emits_error_event() -> None:
    set_router(_make_router())

    class _StubProvider:
        async def predict(self, state):
            return {"cost_usd": 1.0, "duration_sec": 100.0, "tokens": 1000}  # 故意预测大

    class _StubUpdater:
        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []

        async def record(self, **kwargs):
            self.records.append(kwargs)

    updater = _StubUpdater()
    orch = Orchestrator(
        output_translator=_identity_translator,
        prediction_provider=_StubProvider(),
        model_updater=updater,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("hello"):
        events.append((ev.kind, ev.data))

    pc_errors = [data for kind, data in events if kind == "pc_error"]
    assert len(pc_errors) >= 1
    # actual cost ≈ 0 (stub), expected 1.0 → error 应该是负 (actual - expected < 0)
    assert pc_errors[0]["error"]["cost_usd"] < 0
    # updater 真被调
    assert len(updater.records) >= 1
    assert "expected" in updater.records[0]
    assert "actual" in updater.records[0]
    assert "error" in updater.records[0]


# ---- predict 抛异常 → 静默, 不破 step ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pc_provider_exception_doesnt_break_step() -> None:
    set_router(_make_router())

    class _CrashProvider:
        async def predict(self, state):
            raise RuntimeError("simulated provider crash")

    orch = Orchestrator(
        output_translator=_identity_translator,
        prediction_provider=_CrashProvider(),
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("hello"):
        events.append((ev.kind, ev.data))

    # task 应该正常完成
    assert any(k == "done" for k, _ in events)
    # 没 pc_expected event (predict 抛了)
    assert not any(k == "pc_expected" for k, _ in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pc_updater_exception_doesnt_break_step() -> None:
    set_router(_make_router())

    class _StubProvider:
        async def predict(self, state):
            return {"cost_usd": 0.01}

    class _CrashUpdater:
        async def record(self, **kwargs):
            raise RuntimeError("simulated updater crash")

    orch = Orchestrator(
        output_translator=_identity_translator,
        prediction_provider=_StubProvider(),
        model_updater=_CrashUpdater(),
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("hello"):
        events.append((ev.kind, ev.data))

    assert any(k == "done" for k, _ in events)


# ---- 注入参数兼容现有 tests ----


def test_orchestrator_init_accepts_pc_args() -> None:
    """新参数 prediction_provider/model_updater 不破现有 init."""
    orch = Orchestrator()  # 无参数也 work
    assert orch.prediction_provider is None
    assert orch.model_updater is None
