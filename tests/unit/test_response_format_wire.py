"""V2.2 Wire 11 — response_format strict mode 测试.

验证 LLMRequest.response_format 字段贯通到 provider:
- StubProvider 不支持时静默忽略 (向后兼容)
- StructuredStepGenerator 自动传 ExecutionStep schema
- Anthropic provider tool calling 模拟 (mock 验证)
- OpenAI provider 原生 response_format (mock 验证)
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.engineering.execution_protocol import ExecutionStep, _build_request
from kun.interface.llm import LLMMessage, LLMRequest

# ---- LLMRequest 字段 ----


def test_llm_request_default_response_format_none() -> None:
    req = LLMRequest(messages=[LLMMessage(role="user", content="x")])
    assert req.response_format is None


def test_llm_request_accepts_response_format() -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="x")],
        response_format=schema,
    )
    assert req.response_format == schema


# ---- StructuredStepGenerator 自动加 response_format ----


def test_structured_step_generator_attaches_response_format() -> None:
    """_build_request (SMART/MAX 模式) 应该把 ExecutionStep schema 塞进 response_format."""
    req = _build_request("test prompt", {"risk_level": "low"}, "SMART")
    assert req.response_format is not None
    assert req.response_format["type"] == "json_schema"
    assert req.response_format["json_schema"]["name"] == "execution_step"
    schema = req.response_format["json_schema"]["schema"]
    # 应该是 ExecutionStep 的 model_json_schema
    assert "properties" in schema
    assert "thought" in schema["properties"]
    assert "action_type" in schema["properties"]


def test_max_mode_also_attaches_response_format() -> None:
    req = _build_request("test prompt", {"risk_level": "high"}, "MAX")
    assert req.response_format is not None


# ---- Anthropic provider tool calling 模拟 ----


@pytest.mark.asyncio
async def test_anthropic_response_format_via_tool_calling() -> None:
    """Anthropic provider 收到 response_format 应该构造 structured_output 虚拟 tool."""
    from kun.interface.llm.anthropic_provider import AnthropicProvider

    captured_kwargs: dict[str, Any] = {}

    class FakeClient:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kwargs):
                captured_kwargs.update(kwargs)

                class _Resp:
                    content = [  # noqa: RUF012
                        type(
                            "Block",
                            (),
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "structured_output",
                                "input": {"thought": "hi", "action_type": "direct_llm"},
                            },
                        )()
                    ]
                    usage = type(
                        "U",
                        (),
                        {
                            "input_tokens": 5,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    )()
                    stop_reason = "tool_use"

                return _Resp()

    provider = AnthropicProvider(model_id="stub", tier="strong")
    provider._client = FakeClient()  # type: ignore[assignment]

    schema = ExecutionStep.model_json_schema()
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="hi")],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "x", "schema": schema},
        },
    )
    resp = await provider.invoke(req)

    # provider 应该把 schema 包成 structured_output tool
    assert "tools" in captured_kwargs
    assert captured_kwargs["tools"][0]["name"] == "structured_output"
    assert captured_kwargs["tool_choice"]["name"] == "structured_output"
    # 返 content 应该含 tool input json (调用方 parse 用)
    assert "thought" in resp.content
    assert "direct_llm" in resp.content


# ---- OpenAI provider 原生 ----


@pytest.mark.asyncio
async def test_openai_response_format_passes_through() -> None:
    """OpenAI provider 收到 response_format 直接透传."""
    from kun.interface.llm.openai_provider import OpenAIProvider

    captured_kwargs: dict[str, Any] = {}

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    captured_kwargs.update(kwargs)

                    class _Choice:
                        class message:  # noqa: N801
                            content = '{"thought":"x","action_type":"direct_llm"}'
                            tool_calls = None

                        finish_reason = "stop"

                    class _Resp:
                        choices = [_Choice()]  # noqa: RUF012
                        usage = type(
                            "U",
                            (),
                            {
                                "prompt_tokens": 5,
                                "completion_tokens": 3,
                                "prompt_tokens_details": None,
                            },
                        )()
                        model = "stub"

                    return _Resp()

    provider = OpenAIProvider(model_id="stub", tier="strong")
    provider._client = FakeClient()  # type: ignore[assignment]

    schema_block = {
        "type": "json_schema",
        "json_schema": {"name": "x", "schema": {"type": "object"}},
    }
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="hi")],
        response_format=schema_block,
    )
    await provider.invoke(req)

    assert captured_kwargs.get("response_format") == schema_block


# ---- StubProvider 兼容 ----


@pytest.mark.asyncio
async def test_stub_provider_ignores_response_format() -> None:
    """老 StubProvider 不识别 response_format → 不应 crash."""
    from kun.interface.llm.stub_provider import StubProvider

    provider = StubProvider(model_id="stub", tier="cheap", latency_ms=0.0)
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="hi")],
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
    )
    resp = await provider.invoke(req)
    # stub 返默认 content, 不 crash
    assert resp.content is not None
