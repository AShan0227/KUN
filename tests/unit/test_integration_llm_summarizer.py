"""Unit tests for kun.integration.llm_summarizer.

Verifies the LLMRouter → ConversationCompactor.Summarizer bridge:

  - Happy path: ``response.content`` returned verbatim.
  - ``purpose='summarization'`` (or override) reaches ``router.invoke``.
  - System prompt carries ``=== GOAL ANCHOR ===`` block when an anchor is
    supplied; absent when ``anchor=None``.
  - ``messages_to_compact`` are serialized into the user turn — count + first
    and last role/content visible to the model.
  - ``router.invoke`` raises → fallback rule-based summary returned (non-empty
    str), promise of the Summarizer signature preserved.
  - ``max_summary_tokens`` flows into ``LLMRequest.max_tokens``.
  - ``temperature`` flows into ``LLMRequest.temperature``.
  - Empty response → fallback (defensive, not in the spec but cheap to check).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from kun.integration.llm_summarizer import make_llm_summarizer
from kun.interface.llm.base import LLMRequest, LLMResponse, UsageInfo


class _StubRouter:
    """Async-invoke stand-in for LLMRouter. Records calls; returns canned response.

    Optionally raises ``raise_exc`` on every ``invoke`` to exercise the
    fallback path.
    """

    def __init__(
        self,
        response: LLMResponse | None = None,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response = response or LLMResponse(
            content="LLM summary text", model="stub", provider="stub"
        )
        self._raise_exc = raise_exc
        self.calls: list[tuple[LLMRequest, str]] = []

    async def invoke(self, request: LLMRequest, *, purpose: str = "execution") -> LLMResponse:
        self.calls.append((request, purpose))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _make_response(content: str = "ok") -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=UsageInfo(input_tokens=10, output_tokens=20),
        model="stub-cheap",
        provider="stub",
    )


# ---- 1. Happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_response_content() -> None:
    router = _StubRouter(_make_response("compressed summary"))
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    out = await summarizer(
        [{"role": "user", "content": "old turn"}, {"role": "assistant", "content": "reply"}],
        None,
    )

    assert out == "compressed summary"
    assert len(router.calls) == 1


# ---- 2. Purpose flows through (default = 'summarization') ---------------------


@pytest.mark.asyncio
async def test_default_purpose_is_summarization() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    _, purpose = router.calls[0]
    assert purpose == "summarization"


@pytest.mark.asyncio
async def test_custom_purpose_overrides_default() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router, purpose="compression")  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    _, purpose = router.calls[0]
    assert purpose == "compression"


# ---- 3. Anchor block in system prompt when anchor provided --------------------


@pytest.mark.asyncio
async def test_anchor_block_included_when_anchor_provided() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    anchor = {
        "goal_statement": "Ship the v1 release",
        "success_criteria": ["tests green", "deploy clean"],
    }
    await summarizer([{"role": "user", "content": "x"}], anchor)

    request, _ = router.calls[0]
    system_msg = request.messages[0]
    assert system_msg.role == "system"
    assert "=== GOAL ANCHOR ===" in system_msg.content
    assert "Ship the v1 release" in system_msg.content
    assert "tests green" in system_msg.content


# ---- 4. Anchor block absent when anchor=None ---------------------------------


@pytest.mark.asyncio
async def test_anchor_block_absent_when_anchor_none() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    request, _ = router.calls[0]
    system_msg = request.messages[0]
    assert system_msg.role == "system"
    assert "=== GOAL ANCHOR ===" not in system_msg.content
    # Sanity: it's still the compression instruction prompt.
    assert "压缩" in system_msg.content


# ---- 5. Messages serialized into user turn (count + ends visible) ------------


@pytest.mark.asyncio
async def test_messages_serialized_into_user_turn() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "last reply"},
    ]
    await summarizer(messages, None)

    request, _ = router.calls[0]
    # system + user → exactly two messages
    assert len(request.messages) == 2
    user_msg = request.messages[1]
    assert user_msg.role == "user"

    # Header announces the count.
    assert "请压缩以下 4 条消息" in user_msg.content

    # JSON body parses and round-trips role/content for first + last entries.
    payload_start = user_msg.content.index("[")
    payload = json.loads(user_msg.content[payload_start:])
    assert isinstance(payload, list)
    assert len(payload) == 4
    assert payload[0] == {"role": "user", "content": "first question"}
    assert payload[-1] == {"role": "assistant", "content": "last reply"}


# ---- 6. Fallback when router raises ------------------------------------------


@pytest.mark.asyncio
async def test_router_exception_triggers_rule_based_fallback() -> None:
    router = _StubRouter(raise_exc=RuntimeError("provider down"))
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "user said something important"},
        {"role": "assistant", "content": "assistant replied with a long answer"},
    ]
    out = await summarizer(messages, None)

    # 1. Non-empty string returned (Summarizer signature contract).
    assert isinstance(out, str)
    assert out.strip()
    # 2. Tagged as fallback so log / replay tooling can detect it.
    assert "[fallback summary]" in out
    # 3. Hints from the original messages should still surface (truncated).
    assert "user said" in out
    assert "assistant replied" in out


@pytest.mark.asyncio
async def test_router_exception_fallback_uses_anchor_recap() -> None:
    router = _StubRouter(raise_exc=TimeoutError("network"))
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    anchor = {"goal_statement": "Finalize the audit report by Friday"}
    out = await summarizer([{"role": "user", "content": "hi"}], anchor)

    assert "[fallback summary]" in out
    assert "Finalize the audit report" in out  # anchor recap line


# ---- 7. max_summary_tokens flows into LLMRequest.max_tokens ------------------


@pytest.mark.asyncio
async def test_max_summary_tokens_flows_into_request() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router, max_summary_tokens=1234)  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    request, _ = router.calls[0]
    assert request.max_tokens == 1234
    # Budget should also appear in the system prompt for the model to see.
    assert "1234" in request.messages[0].content


# ---- 8. temperature flows into LLMRequest.temperature ------------------------


@pytest.mark.asyncio
async def test_temperature_flows_into_request() -> None:
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router, temperature=0.05)  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    request, _ = router.calls[0]
    assert request.temperature == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_default_temperature_is_low() -> None:
    """Default 0.2 keeps summaries near-deterministic."""
    router = _StubRouter(_make_response())
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    await summarizer([{"role": "user", "content": "x"}], None)

    request, _ = router.calls[0]
    assert request.temperature == pytest.approx(0.2)


# ---- 9. Empty response → fallback (defensive) --------------------------------


@pytest.mark.asyncio
async def test_empty_response_falls_back_to_rule_based() -> None:
    """A whitespace-only summary is just as broken as a raise — fall back."""
    router = _StubRouter(_make_response(content="   "))
    summarizer = make_llm_summarizer(router)  # type: ignore[arg-type]

    out = await summarizer(
        [{"role": "user", "content": "valuable content"}],
        None,
    )
    assert out.strip()
    assert "[fallback summary]" in out
