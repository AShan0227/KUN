"""Unit tests for kun.integration.llm_invoker.

These tests verify the LLMRouter → ExecutorLoop.LLMInvoker bridge:

  - Field renames (ToolCall.id → tool_id, cost_usd_equivalent → cost_usd)
  - usage.total() rollup
  - finish_reason passthrough across all four values
  - LLMRequest construction (purpose, profile, tools, temperature, max_tokens)
  - Dict-to-LLMMessage filtering (drops _kun_compacted, tool_calls,
    is_error, tool_id and similar exec_loop bookkeeping fields)
  - Multi-tool response field-by-field mapping
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.executor.exec_loop import LLMStepResponse
from kun.agents.executor.exec_loop import ToolCall as ExecToolCall
from kun.integration.llm_invoker import make_llm_invoker
from kun.interface.llm.base import (
    LLMRequest,
    LLMResponse,
    TaskProfile,
    ToolCall,
    ToolSpec,
    UsageInfo,
)


class _StubRouter:
    """Pure-Python stand-in for LLMRouter — no real provider or HTTP I/O.

    Records the (request, purpose) tuple every time ``invoke`` is awaited so
    tests can assert on the LLMRequest the adapter constructed.
    """

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls: list[tuple[LLMRequest, str]] = []

    async def invoke(self, request: LLMRequest, *, purpose: str = "execution") -> LLMResponse:
        self.calls.append((request, purpose))
        return self._response


def _make_response(
    *,
    content: str = "ok",
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
    usage: UsageInfo | None = None,
    cost_usd_actual: float = 0.0,
    cost_usd_equivalent: float = 0.0,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,  # type: ignore[arg-type]
        usage=usage or UsageInfo(),
        cost_usd_actual=cost_usd_actual,
        cost_usd_equivalent=cost_usd_equivalent,
        model="stub-model",
        provider="stub",
    )


# ---- 1. Happy path ----


@pytest.mark.asyncio
async def test_happy_path_basic_content_passthrough() -> None:
    router = _StubRouter(_make_response(content="hello world"))
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]

    result = await invoker([{"role": "user", "content": "hi"}])

    assert isinstance(result, LLMStepResponse)
    assert result.content == "hello world"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert len(router.calls) == 1


# ---- 2. tool_calls field rename ----


@pytest.mark.asyncio
async def test_tool_calls_field_rename_id_to_tool_id() -> None:
    """LLMResponse.ToolCall has ``id``; exec_loop.ToolCall has ``tool_id``."""
    router = _StubRouter(
        _make_response(
            content="",
            tool_calls=[
                ToolCall(id="call-abc", name="calculator", arguments={"x": 2}),
            ],
            finish_reason="tool_use",
        )
    )
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    result = await invoker([{"role": "user", "content": "do math"}])

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert isinstance(tc, ExecToolCall)
    assert tc.tool_id == "call-abc"  # renamed from .id
    assert tc.name == "calculator"
    assert tc.arguments == {"x": 2}


# ---- 3. finish_reason passthrough (all 4 values) ----


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "tool_use", "length", "error"])
async def test_finish_reason_passthrough_all_values(reason: str) -> None:
    router = _StubRouter(_make_response(finish_reason=reason))
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    result = await invoker([{"role": "user", "content": "x"}])
    assert result.finish_reason == reason


# ---- 4. usage_tokens = response.usage.total() ----


@pytest.mark.asyncio
async def test_usage_tokens_sums_input_and_output() -> None:
    router = _StubRouter(
        _make_response(usage=UsageInfo(input_tokens=120, output_tokens=80))
    )
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    result = await invoker([{"role": "user", "content": "x"}])
    # UsageInfo.total() returns input + output (NOT cached).
    assert result.usage_tokens == 200


# ---- 5. cost_usd = response.cost_usd_equivalent (NOT cost_usd_actual) ----


@pytest.mark.asyncio
async def test_cost_uses_equivalent_not_actual() -> None:
    """ADR-008: cost_usd_equivalent is canonical across subscription/API providers.

    cost_usd_actual is the provider-native cost (often 0 for subscription
    routes); the adapter must use cost_usd_equivalent so budget accounting
    in ExecutorLoop reflects real economic value.
    """
    router = _StubRouter(
        _make_response(cost_usd_actual=0.0, cost_usd_equivalent=0.42)
    )
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    result = await invoker([{"role": "user", "content": "x"}])
    assert result.cost_usd == pytest.approx(0.42)
    # And not 0.0 (which is the cost_usd_actual value)
    assert result.cost_usd != 0.0


# ---- 6. LLMRequest construction (purpose / profile / tools / temp / max_tokens) ----


@pytest.mark.asyncio
async def test_request_construction_all_kwargs_flow_through() -> None:
    router = _StubRouter(_make_response())
    profile = TaskProfile(task_type="report", risk_level="high", needs_reasoning=True)
    tools = [
        ToolSpec(name="calculator", description="adds numbers"),
        ToolSpec(name="search", description="web search"),
    ]
    invoker = make_llm_invoker(
        router,  # type: ignore[arg-type]
        purpose="planning",
        profile=profile,
        tools=tools,
        temperature=0.25,
        max_tokens=512,
    )

    await invoker([{"role": "user", "content": "plan something"}])

    assert len(router.calls) == 1
    request, purpose = router.calls[0]
    assert purpose == "planning"
    assert request.temperature == pytest.approx(0.25)
    assert request.max_tokens == 512
    assert request.profile is profile
    assert len(request.tools) == 2
    assert [t.name for t in request.tools] == ["calculator", "search"]


@pytest.mark.asyncio
async def test_default_purpose_is_execution() -> None:
    """ADR-002: default purpose is 'execution' (Opus 4.7 main path)."""
    router = _StubRouter(_make_response())
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    await invoker([{"role": "user", "content": "x"}])
    assert router.calls[0][1] == "execution"


@pytest.mark.asyncio
async def test_tools_none_becomes_empty_list() -> None:
    router = _StubRouter(_make_response())
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    await invoker([{"role": "user", "content": "x"}])
    assert router.calls[0][0].tools == []


# ---- 7. Messages with extra keys get filtered cleanly ----


@pytest.mark.asyncio
async def test_extra_keys_filtered_from_messages() -> None:
    """Executor decorates messages with bookkeeping fields that LLMMessage
    doesn't accept. The adapter must strip them so model_validate succeeds
    and the LLM never sees internal markers."""
    router = _StubRouter(_make_response())
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "ask",
            "_kun_compacted": True,
            "unknown_extra": 42,
        },
        {
            "role": "assistant",
            "content": "thinking…",
            "tool_calls": [{"tool_id": "t1", "name": "x", "arguments": {}}],
        },
        {
            "role": "tool",
            "tool_id": "t1",
            "content": "ok",
            "is_error": False,
        },
    ]

    # Should not raise — extra keys are dropped before pydantic sees them.
    await invoker(messages)
    assert len(router.calls) == 1
    sent_messages = router.calls[0][0].messages
    assert len(sent_messages) == 3

    # User turn keeps content, drops extras.
    assert sent_messages[0].role == "user"
    assert sent_messages[0].content == "ask"

    # Assistant turn keeps content; ``tool_calls`` not on LLMMessage so it's
    # silently absent. Content survives.
    assert sent_messages[1].role == "assistant"
    assert "thinking" in sent_messages[1].content

    # Tool turn: tool_id is lifted into tool_call_id; content gains the
    # "Tool result [t1]: " prefix.
    assert sent_messages[2].role == "tool"
    assert sent_messages[2].tool_call_id == "t1"
    assert "Tool result" in sent_messages[2].content
    assert "ok" in sent_messages[2].content


@pytest.mark.asyncio
async def test_tool_message_with_is_error_prefixes_error_label() -> None:
    router = _StubRouter(_make_response())
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]

    messages: list[dict[str, Any]] = [
        {
            "role": "tool",
            "tool_id": "t-7",
            "content": "boom",
            "is_error": True,
        },
    ]
    await invoker(messages)
    sent = router.calls[0][0].messages[0]
    assert sent.tool_call_id == "t-7"
    assert "Tool error" in sent.content
    assert "boom" in sent.content


# ---- 8. Multi-tool response (≥2 tool_calls all mapped) ----


@pytest.mark.asyncio
async def test_multiple_tool_calls_all_mapped() -> None:
    router = _StubRouter(
        _make_response(
            content="",
            tool_calls=[
                ToolCall(id="call-1", name="a", arguments={"i": 1}),
                ToolCall(id="call-2", name="b", arguments={"j": 2, "k": "v"}),
                ToolCall(id="call-3", name="c", arguments={}),
            ],
            finish_reason="tool_use",
        )
    )
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]
    result = await invoker([{"role": "user", "content": "do many things"}])

    assert len(result.tool_calls) == 3
    assert [tc.tool_id for tc in result.tool_calls] == ["call-1", "call-2", "call-3"]
    assert [tc.name for tc in result.tool_calls] == ["a", "b", "c"]
    assert result.tool_calls[0].arguments == {"i": 1}
    assert result.tool_calls[1].arguments == {"j": 2, "k": "v"}
    assert result.tool_calls[2].arguments == {}


# ---- Bonus: messages list passed verbatim length ----


@pytest.mark.asyncio
async def test_message_count_preserved() -> None:
    router = _StubRouter(_make_response())
    invoker = make_llm_invoker(router)  # type: ignore[arg-type]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    await invoker(messages)

    sent = router.calls[0][0].messages
    assert len(sent) == 4
    assert [m.role for m in sent] == ["system", "user", "assistant", "user"]
