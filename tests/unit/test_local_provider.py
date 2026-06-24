"""L2.4 — LocalLLMProvider 单测 (无外部依赖, mock OpenAI client)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.local_provider import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    LocalLLMProvider,
)


def test_defaults() -> None:
    p = LocalLLMProvider()
    assert p.name == "local"
    assert p.model_id == DEFAULT_MODEL_ID
    assert p.tier == "cheap"
    assert p.price_input_per_mtok == 0.0
    assert p.price_output_per_mtok == 0.0
    assert p.supports_tools is False
    assert p.supports_cache is False
    # 默认 base_url ollama
    assert p._base_url == DEFAULT_BASE_URL


def test_custom_endpoint() -> None:
    p = LocalLLMProvider(
        model_id="llama3.1:70b", base_url="http://other-host:8080/v1", timeout_sec=60
    )
    assert p.model_id == "llama3.1:70b"
    assert p._base_url == "http://other-host:8080/v1"


def _make_fake_response(content: str, prompt_tokens: int = 5, completion_tokens: int = 8):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


@pytest.mark.asyncio
async def test_invoke_happy_path() -> None:
    p = LocalLLMProvider()
    fake = _make_fake_response("hello from local")
    with patch.object(
        p._client.chat.completions, "create", new=AsyncMock(return_value=fake)
    ):
        request = LLMRequest(
            messages=[LLMMessage(role="user", content="hi")],
            max_tokens=64,
            temperature=0.3,
        )
        response = await p.invoke(request)

    assert response.content == "hello from local"
    assert response.provider == "local"
    assert response.tier == "cheap"
    assert response.model == DEFAULT_MODEL_ID
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 8
    assert response.cost_usd_actual == 0.0
    assert response.cost_usd_equivalent == 0.0
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_invoke_passes_stop_words() -> None:
    p = LocalLLMProvider()
    fake = _make_fake_response("done")
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return fake

    with patch.object(p._client.chat.completions, "create", new=fake_create):
        await p.invoke(
            LLMRequest(
                messages=[LLMMessage(role="user", content="x")],
                stop=["</done>"],
            )
        )

    assert captured["stop"] == ["</done>"]
    assert captured["model"] == DEFAULT_MODEL_ID


@pytest.mark.asyncio
async def test_invoke_length_finish_reason() -> None:
    p = LocalLLMProvider()
    fake = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="truncated", tool_calls=None),
                finish_reason="length",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=10),
    )
    with patch.object(
        p._client.chat.completions, "create", new=AsyncMock(return_value=fake)
    ):
        resp = await p.invoke(
            LLMRequest(messages=[LLMMessage(role="user", content="y")])
        )
    assert resp.finish_reason == "length"


@pytest.mark.asyncio
async def test_invoke_propagates_underlying_exception() -> None:
    p = LocalLLMProvider()
    with patch.object(
        p._client.chat.completions,
        "create",
        new=AsyncMock(side_effect=ConnectionError("ollama down")),
    ), pytest.raises(ConnectionError):
        await p.invoke(
            LLMRequest(messages=[LLMMessage(role="user", content="z")])
        )


@pytest.mark.asyncio
async def test_health_check_returns_false_on_failure() -> None:
    p = LocalLLMProvider()
    with patch.object(
        p._client.chat.completions,
        "create",
        new=AsyncMock(side_effect=ConnectionError("not running")),
    ):
        assert await p.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_true_on_success() -> None:
    p = LocalLLMProvider()
    fake = _make_fake_response("pong")
    with patch.object(
        p._client.chat.completions, "create", new=AsyncMock(return_value=fake)
    ):
        assert await p.health_check() is True
