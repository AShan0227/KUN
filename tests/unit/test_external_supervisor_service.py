"""L2.4 — ExternalSupervisorService 单测 (注入 stub LLM provider)."""

from __future__ import annotations

import asyncio
import json

import pytest
from kun.external_supervisor.service import (
    ExternalSupervisorObservation,
    ExternalSupervisorService,
    _coerce_verdict,
    _parse_llm_response,
)
from kun.interface.llm.stub_provider import StubProvider


def _stub_returning(content: str) -> StubProvider:
    """Stub provider 固定返回 content."""
    from kun.interface.llm.base import LLMResponse, UsageInfo

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


# --- parser unit ---


def test_parse_clean_json() -> None:
    raw = json.dumps(
        {"verdict": "ok", "rationale": "looks fine", "recommended_action": None}
    )
    parsed = _parse_llm_response(raw)
    assert parsed["verdict"] == "ok"
    assert parsed["rationale"] == "looks fine"


def test_parse_json_embedded_in_prose() -> None:
    raw = 'Sure! Here is my analysis: {"verdict": "concerning", "rationale": "drift"} cheers'
    parsed = _parse_llm_response(raw)
    assert parsed["verdict"] == "concerning"


def test_parse_falls_back_to_heuristic() -> None:
    raw = "The system is alarming because it fabricated a result."
    parsed = _parse_llm_response(raw)
    assert parsed["verdict"] == "alarming"
    assert parsed.get("_parsed_via") == "heuristic"


def test_parse_empty_response_is_concerning() -> None:
    parsed = _parse_llm_response("")
    assert parsed["verdict"] == "concerning"


# --- coerce verdict ---


def test_coerce_verdict_canonical() -> None:
    assert _coerce_verdict("ok") == "ok"
    assert _coerce_verdict("alarming") == "alarming"


def test_coerce_verdict_aliases() -> None:
    assert _coerce_verdict("warn") == "concerning"
    assert _coerce_verdict("critical") == "alarming"
    assert _coerce_verdict("pass") == "ok"


def test_coerce_verdict_unknown_defaults_concerning() -> None:
    assert _coerce_verdict("???") == "concerning"
    assert _coerce_verdict(None) == "concerning"


# --- service ---


@pytest.mark.asyncio
async def test_analyze_observation_happy_path() -> None:
    stub = _stub_returning(
        json.dumps(
            {
                "verdict": "ok",
                "rationale": "task output matches success criteria",
                "recommended_action": None,
            }
        )
    )
    service = ExternalSupervisorService(llm_provider=stub)
    obs = await service.analyze_observation(
        obs_kind="task_complete",
        observation_payload={"task_id": "t1", "result": "auth middleware shipped"},
        anchor={
            "goal_statement": "ship auth middleware",
            "success_criteria": ["tests pass", "no PII leak"],
        },
        target_task_id="t1",
        target_anchor_id="ga1",
    )
    assert isinstance(obs, ExternalSupervisorObservation)
    assert obs.verdict == "ok"
    assert obs.rationale == "task output matches success criteria"
    assert obs.target_task_id == "t1"
    assert obs.target_anchor_id == "ga1"
    assert obs.model_used == "stub-1"
    assert obs.extras["parsed_via"] == "json"


@pytest.mark.asyncio
async def test_analyze_observation_alarming_aliases_normalized() -> None:
    stub = _stub_returning(json.dumps({"verdict": "critical", "rationale": "fab"}))
    service = ExternalSupervisorService(llm_provider=stub)
    obs = await service.analyze_observation(
        obs_kind="drift_check",
        observation_payload={"foo": "bar"},
    )
    assert obs.verdict == "alarming"


@pytest.mark.asyncio
async def test_analyze_observation_heuristic_fallback() -> None:
    stub = _stub_returning("This observation is concerning — drift detected.")
    service = ExternalSupervisorService(llm_provider=stub)
    obs = await service.analyze_observation(
        obs_kind="drift_check", observation_payload={}
    )
    assert obs.verdict == "concerning"
    assert obs.extras["parsed_via"] == "heuristic"


@pytest.mark.asyncio
async def test_analyze_observation_propagates_llm_failure() -> None:
    failing = StubProvider(latency_ms=0, fail_rate=1.0)
    service = ExternalSupervisorService(llm_provider=failing)
    with pytest.raises(RuntimeError):
        await service.analyze_observation(
            obs_kind="task_complete", observation_payload={}
        )


@pytest.mark.asyncio
async def test_analyze_observation_includes_anchor_in_prompt() -> None:
    """anchor 必须进 prompt 顶部 (capture system message via recording builder)."""
    captured: list[str] = []

    def recording_builder(request):
        from kun.interface.llm.base import LLMResponse, UsageInfo

        captured.append(request.messages[0].content)
        return LLMResponse(
            content='{"verdict": "ok", "rationale": "fine"}',
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    stub = StubProvider(model_id="stub-1", tier="cheap", latency_ms=0, builder=recording_builder)
    service = ExternalSupervisorService(llm_provider=stub)
    await service.analyze_observation(
        obs_kind="gate_review",
        observation_payload={"x": 1},
        anchor={
            "goal_statement": "make authentication safer",
            "success_criteria": ["pass pentest"],
            "out_of_scope": ["refactor billing"],
            "invariants": ["no PII to logs"],
        },
    )
    system_prompt = captured[-1]
    assert "EXTERNAL SUPERVISOR" in system_prompt
    assert "make authentication safer" in system_prompt
    assert "pass pentest" in system_prompt
    assert "refactor billing" in system_prompt
    assert "no PII to logs" in system_prompt
    assert "gate_review" in system_prompt


@pytest.mark.asyncio
async def test_concurrency_limited_by_semaphore() -> None:
    """max_concurrent=1 时同时 2 个调用应串行."""
    started = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def slow_invoke(request):
        nonlocal started, max_in_flight
        async with lock:
            started += 1
            max_in_flight = max(max_in_flight, started)
        await asyncio.sleep(0.02)
        from kun.interface.llm.base import LLMResponse, UsageInfo

        async with lock:
            started -= 1
        return LLMResponse(
            content='{"verdict": "ok"}',
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    class SlowStub:
        name = "stub"
        model_id = "stub-1"
        tier = "cheap"

        async def invoke(self, req):
            return await slow_invoke(req)

    service = ExternalSupervisorService(llm_provider=SlowStub(), max_concurrent=1)  # type: ignore[arg-type]
    await asyncio.gather(
        service.analyze_observation(obs_kind="x", observation_payload={}),
        service.analyze_observation(obs_kind="x", observation_payload={}),
    )
    # Should have run serially → never 2 in flight
    assert max_in_flight == 1


def test_invalid_max_concurrent() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        ExternalSupervisorService(llm_provider=_stub_returning("{}"), max_concurrent=0)
