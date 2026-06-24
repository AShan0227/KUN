"""AnthropicProvider maps role="tool" to a tool_result block (audit F044).

role="tool" is not a valid Anthropic role; tool results must be a tool_result
content block inside a user message, keyed by tool_use_id. The old code passed
role="tool" straight through, so any multi-turn tool loop 400'd.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.anthropic_provider import _to_anthropic_message
from kun.interface.llm.base import LLMMessage


@pytest.mark.unit
def test_tool_message_becomes_user_tool_result() -> None:
    m = LLMMessage(role="tool", content="42 rows", tool_call_id="toolu_abc")
    out = _to_anthropic_message(m)
    assert out["role"] == "user"  # NOT "tool"
    assert isinstance(out["content"], list)
    block = out["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_abc"
    assert block["content"] == "42 rows"


@pytest.mark.unit
def test_no_role_tool_ever_emitted() -> None:
    # The whole point of F044: the Anthropic API rejects role="tool".
    for role in ("user", "assistant", "tool"):
        out = _to_anthropic_message(LLMMessage(role=role, content="x", tool_call_id="t"))
        assert out["role"] != "tool"


@pytest.mark.unit
def test_plain_user_and_assistant_passthrough() -> None:
    u = _to_anthropic_message(LLMMessage(role="user", content="hi"))
    assert u == {"role": "user", "content": "hi"}
    a = _to_anthropic_message(LLMMessage(role="assistant", content="hello"))
    assert a == {"role": "assistant", "content": "hello"}


@pytest.mark.unit
def test_cache_wraps_text_block() -> None:
    out = _to_anthropic_message(LLMMessage(role="user", content="ctx", cache=True))
    assert out["content"][0]["type"] == "text"
    assert out["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.unit
def test_tool_message_without_id_does_not_crash() -> None:
    out = _to_anthropic_message(LLMMessage(role="tool", content="r"))
    assert out["content"][0]["tool_use_id"] == ""
