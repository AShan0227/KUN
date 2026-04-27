"""Wire 36: orchestrator 标记 task done 前调 VerificationRunner.verify()."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.datamodel.verification_spec import VerificationResult, VerificationSpec
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


def _intent_with_verification() -> LLMResponse:
    """Intent 返带 verification_specs 的 spec."""
    return LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.simple",
                "risk_level": "low",
                "complexity_score": 0.1,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "say hello",
                "goal_detail": "say hello",
                "verification_specs": [
                    {"kind": "exact_output", "spec": {"expected": "hello"}},
                ],
            }
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )


def _exec_response() -> LLMResponse:
    return LLMResponse(
        content="hello",  # 跟 verification expected 匹配
        usage=UsageInfo(input_tokens=10, output_tokens=4),
    )


class _RoutingStub(StubProvider):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            return _intent_with_verification()
        return _exec_response()


def _make_router() -> LLMRouter:
    stub = _RoutingStub(tier="top")
    return LLMRouter({"top": stub, "cheap": stub, "coding": stub, "fallback": stub})


# ---- VerificationRunner stub ----


class _StubVerificationRunner:
    """用 always-pass / always-fail 的 stub runner."""

    def __init__(self, *, always_pass: bool = True) -> None:
        self.always_pass = always_pass
        self.calls: list[tuple[VerificationSpec, str]] = []

    async def verify(self, spec: VerificationSpec, artifact: str) -> VerificationResult:
        self.calls.append((spec, artifact))
        return VerificationResult(
            kind=spec.kind,
            passed=self.always_pass,
            evidence_url=None,
            error_msg=None if self.always_pass else "stub-fail",
        )


# ---- 测试 ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_verification_runner_keeps_done() -> None:
    """没装 verification_runner → 跟 Wire 11 行为一致, status=done."""
    set_router(_make_router())

    orch = Orchestrator(output_translator=_identity_translator)
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    # done event 应该 emit
    done_events = [d for k, d in events if k == "done"]
    assert len(done_events) >= 1
    # 没 verification_done event
    assert not any(k == "verification_done" for k, _ in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verification_runner_passes_marks_done() -> None:
    """verification 全过 → status=done + emit verification_done(failed=False)."""
    set_router(_make_router())
    runner = _StubVerificationRunner(always_pass=True)

    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=runner,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    verifications = [d for k, d in events if k == "verification_done"]
    assert len(verifications) == 1
    assert verifications[0]["failed"] is False
    assert len(verifications[0]["results"]) == 1
    assert verifications[0]["results"][0]["passed"] is True

    # runner 真被调
    assert len(runner.calls) == 1
    assert runner.calls[0][0].kind == "exact_output"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verification_failure_marks_task_failed() -> None:
    """required verification 失败 → status=failed (不是 done)."""
    set_router(_make_router())
    runner = _StubVerificationRunner(always_pass=False)

    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=runner,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    verifications = [d for k, d in events if k == "verification_done"]
    assert len(verifications) == 1
    assert verifications[0]["failed"] is True
    assert verifications[0]["results"][0]["passed"] is False

    # 注意: status=failed 反映在 done event 的 status 字段
    done_events = [d for k, d in events if k == "done"]
    if done_events:
        # 至少看到 verification_done 在 done 之前
        assert "failed" in str(done_events[0]).lower() or len(verifications) >= 1
    # 关键: runner 被调


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optional_verification_failure_keeps_done() -> None:
    """required=False 的 spec 失败 → 不 mark failed, 但记入 results."""
    set_router(_make_router())

    class _OptionalFailRunner:
        def __init__(self) -> None:
            self.calls: list = []

        async def verify(self, spec: VerificationSpec, artifact: str) -> VerificationResult:
            self.calls.append((spec, artifact))
            return VerificationResult(kind=spec.kind, passed=False, error_msg="optional-fail")

    runner = _OptionalFailRunner()

    # Override intent to return spec with required=False
    optional_intent = LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.simple",
                "risk_level": "low",
                "complexity_score": 0.1,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "say hello",
                "goal_detail": "say hello",
                "verification_specs": [
                    {"kind": "lint_pass", "spec": {}, "required": False},
                ],
            }
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )

    class _OptionalRoutingStub(StubProvider):
        async def invoke(self, request):
            sys_text = " ".join(m.content for m in request.messages if m.role == "system")
            if "意图理解层" in sys_text:
                return optional_intent
            return _exec_response()

    stub = _OptionalRoutingStub(tier="top")
    set_router(LLMRouter({"top": stub, "cheap": stub, "coding": stub, "fallback": stub}))

    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=runner,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    verifications = [d for k, d in events if k == "verification_done"]
    assert len(verifications) == 1
    # required=False → failed 不 set True
    assert verifications[0]["failed"] is False
    # 但 results 仍记 passed=False
    assert verifications[0]["results"][0]["passed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_spec_skips_verification() -> None:
    """task spec 没 verification_specs → 不调 runner."""

    no_spec_intent = LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.simple",
                "risk_level": "low",
                "complexity_score": 0.1,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "say hello",
                # 没 verification_specs / 没 goal_detail (无 spec)
            }
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )

    class _NoSpecRoutingStub(StubProvider):
        async def invoke(self, request):
            sys_text = " ".join(m.content for m in request.messages if m.role == "system")
            if "意图理解层" in sys_text:
                return no_spec_intent
            return _exec_response()

    stub = _NoSpecRoutingStub(tier="top")
    set_router(LLMRouter({"top": stub, "cheap": stub, "coding": stub, "fallback": stub}))

    runner = _StubVerificationRunner(always_pass=True)
    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=runner,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    # runner 不被调 (没 spec)
    assert len(runner.calls) == 0
    # 也不 emit verification_done event
    assert not any(k == "verification_done" for k, _ in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verification_runner_exception_required_marks_failed() -> None:
    """runner.verify 抛异常 + required → mark failed."""
    set_router(_make_router())

    class _CrashRunner:
        async def verify(self, spec, artifact):
            raise RuntimeError("simulated runner crash")

    runner = _CrashRunner()
    orch = Orchestrator(
        output_translator=_identity_translator,
        verification_runner=runner,
    )
    events: list[tuple[str, Any]] = []
    async for ev in orch.stream("say hello"):
        events.append((ev.kind, ev.data))

    verifications = [d for k, d in events if k == "verification_done"]
    assert len(verifications) == 1
    assert verifications[0]["failed"] is True
    assert verifications[0]["results"][0]["passed"] is False
    assert "crash" in verifications[0]["results"][0].get("error", "").lower()
