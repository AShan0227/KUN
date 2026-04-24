"""CodexMcpProvider unit tests.

Notes:
  - Full MCP stdio round-trip is exercised by a live manual check
    (`python -c "from ... import CodexMcpProvider; ..."`); mocking
    ``asyncio.create_subprocess_exec`` reliably is brittle and the resulting
    tests tell you little about real behavior. We keep those checks out of
    the unit layer. When the integration suite grows we'll add a real-codex
    `@pytest.mark.integration` case.
"""

from __future__ import annotations

import os

import pytest
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.codex_mcp_provider import CodexMcpProvider


@pytest.mark.unit
def test_build_prompt_concatenates_all_roles():
    req = LLMRequest(
        messages=[
            LLMMessage(role="system", content="you are helpful"),
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="assistant", content="hello"),
            LLMMessage(role="tool", content='{"ok":true}'),
        ]
    )
    built = CodexMcpProvider._build_prompt(req)
    assert "# System" in built
    assert "you are helpful" in built
    assert "# User" in built
    assert "# Assistant (prior)" in built
    assert "hello" in built
    assert "# Tool result" in built


@pytest.mark.unit
def test_build_prompt_empty_messages_returns_placeholder():
    assert CodexMcpProvider._build_prompt(LLMRequest(messages=[])) == "(empty)"


@pytest.mark.unit
def test_available_is_boolean():
    assert CodexMcpProvider.available() in (True, False)


@pytest.mark.unit
def test_model_id_env_override():
    prev = os.environ.get("KUN_CODEX_MCP_MODEL")
    os.environ["KUN_CODEX_MCP_MODEL"] = "gpt-5.3-codex"
    try:
        p = CodexMcpProvider(tier="coding")
        assert p.model_id == "gpt-5.3-codex"
    finally:
        if prev is None:
            del os.environ["KUN_CODEX_MCP_MODEL"]
        else:
            os.environ["KUN_CODEX_MCP_MODEL"] = prev


@pytest.mark.unit
def test_reasoning_effort_env_override():
    prev = os.environ.get("KUN_CODEX_REASONING")
    os.environ["KUN_CODEX_REASONING"] = "medium"
    try:
        p = CodexMcpProvider(tier="coding")
        assert p.reasoning_effort == "medium"
    finally:
        if prev is None:
            del os.environ["KUN_CODEX_REASONING"]
        else:
            os.environ["KUN_CODEX_REASONING"] = prev


@pytest.mark.unit
def test_default_cost_is_zero_actual():
    """Subscription-paid — cost_usd_actual is always 0."""
    p = CodexMcpProvider(tier="coding")
    assert p.price_input_per_mtok == 0.0
    assert p.price_output_per_mtok == 0.0
    # Equivalent still populated from pricing table for ADR-008 duality
    assert p.equivalent_price_input_per_mtok > 0
    assert p.equivalent_price_output_per_mtok > 0


@pytest.mark.unit
def test_default_cwd_created():
    """Provider creates its sandbox cwd on init so first call doesn't race it."""
    p = CodexMcpProvider(tier="coding")
    assert os.path.isdir(p._cwd)
