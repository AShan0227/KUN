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
from kun.interface.llm.base import LLMMessage, LLMRequest, ToolSpec
from kun.interface.llm.codex_mcp_provider import (
    PURE_LLM_BASE_INSTRUCTIONS,
    CodexMcpProvider,
)


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


@pytest.mark.unit
def test_cwd_and_sandbox_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KUN_CODEX_MCP_CWD", str(tmp_path))
    monkeypatch.setenv("KUN_CODEX_MCP_SANDBOX", "workspace-write")

    p = CodexMcpProvider(tier="coding")

    assert p._cwd == str(tmp_path)
    assert p._sandbox == "workspace-write"


@pytest.mark.unit
def test_explicit_sandbox_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KUN_CODEX_MCP_CWD", str(tmp_path))
    monkeypatch.setenv("KUN_CODEX_MCP_SANDBOX", "read-only")

    p = CodexMcpProvider(tier="coding", sandbox="workspace-write")

    assert p._cwd == str(tmp_path)
    assert p._sandbox == "workspace-write"


@pytest.mark.unit
def test_stream_limit_env_override(monkeypatch):
    monkeypatch.setenv("KUN_CODEX_MCP_STREAM_LIMIT_BYTES", "1048576")

    p = CodexMcpProvider(tier="coding")

    assert p._stream_limit == 1048576


@pytest.mark.unit
def test_stream_limit_invalid_env_uses_default(monkeypatch):
    monkeypatch.setenv("KUN_CODEX_MCP_STREAM_LIMIT_BYTES", "not-a-number")

    p = CodexMcpProvider(tier="coding")

    assert p._stream_limit > 1048576


# =================== LT.CODEX-PURE-LLM ===================
# These tests pin down the contract that codex MCP-server is driven in
# pure-LLM mode: gpt-5.x is told it has no file system / no shell, the host
# owns the tools, and tool requests come back as <skill> XML which KUN's
# llm_invoker parses (LT.TOOLS-GAP path). Catches regressions where someone
# softens the base-instructions or flips supports_tools back to False.


@pytest.mark.unit
def test_supports_tools_is_true_in_pure_llm_mode():
    """In pure-LLM mode codex MCP supports tools via the XML protocol — the
    flag must reflect that so the router and ExecutorLoop know to inject
    skill specs into the prompt."""
    p = CodexMcpProvider(tier="coding")
    assert p.supports_tools is True


@pytest.mark.unit
def test_base_instructions_assert_no_file_access_and_xml_protocol():
    """The base-instructions sent to codex MCP must override codex's default
    agent identity. Required signals: (1) explicit no-write/no-shell, (2)
    XML protocol example, (3) explicit don't-claim-sandbox.

    Regression catcher: dogfood v4 broke because base-instructions said
    "Do not run tools or take side effects" — that left gpt-5.x with two
    conflicting directives (codex's no-tools vs KUN's use-skills) and it
    chose to refuse with a sandbox excuse.
    """
    text = PURE_LLM_BASE_INSTRUCTIONS
    lower = text.lower()
    # LT.TOOLS-GAP-2 revision: distinguishes "no DIRECT codex tools" from
    # "no tools at all". Must explicitly reference host tools / KUN tools.
    assert "host tool" in lower or "kun tool" in lower or "host_tool" in lower
    # Explicit no-codex-direct-tools (any phrasing variant)
    assert "no codex" in lower or "no direct codex" in lower
    # XML protocol shape — model must see the exact <skill name="..."> shape
    # AND the JSON-in-body form (not nested elements)
    assert '<skill name=' in text
    assert '"param1"' in text or '"key"' in text or "JSON" in text or "json" in text
    # Don't claim sandbox restrictions (regression catcher for v4)
    assert "sandbox" in lower
    # Must instruct model NOT to mention/claim sandbox
    assert any(
        signal in lower
        for signal in ("never claim", "never mention", "off-topic")
    )
    # Anti-hallucination: must explicitly forbid invented tool names
    # (regression catcher for v5 where gpt-5.5 made up "presentations" etc)
    assert "invent" in lower or "made-up" in lower or "made up" in lower
    assert "trust the list" in lower or "trust the list" in text.lower()


@pytest.mark.unit
def test_invoke_payload_uses_pure_llm_base_instructions(monkeypatch):
    """The actual JSON-RPC payload sent to codex MCP must carry
    PURE_LLM_BASE_INSTRUCTIONS as ``base-instructions``. We don't run the
    subprocess — we intercept _send and assert the payload shape.
    """
    import asyncio

    p = CodexMcpProvider(tier="coding")

    captured: dict = {}

    async def _fake_send(msg):
        # Capture the first tools/call payload, fire a fake result so invoke
        # can return without hanging.
        if msg.get("method") == "tools/call":
            captured["payload"] = msg
            fut = p._pending.get(msg["id"])
            if fut and not fut.done():
                fut.set_result(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"structuredContent": {"content": "ok"}},
                    }
                )

    async def _fake_ensure_running():
        return None

    monkeypatch.setattr(p, "_send", _fake_send)
    monkeypatch.setattr(p, "_ensure_running", _fake_ensure_running)

    resp = asyncio.run(
        p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    )

    assert resp.content == "ok"
    args = captured["payload"]["params"]["arguments"]
    assert args["base-instructions"] == PURE_LLM_BASE_INSTRUCTIONS
    # Old agent-mode wording must be GONE — this catches a partial revert.
    assert "Do not run tools" not in args["base-instructions"]


@pytest.mark.unit
def test_build_prompt_includes_tool_specs_when_request_carries_tools():
    """When the request has ``tools`` populated, the built prompt must
    surface them as XML protocol description so gpt-5.x sees them even
    when the orchestrator didn't bake a skill_directive into the system
    message. Important for callers that pass tools= directly to the router
    rather than through LongTaskOrchestrator's skill_directive injection.
    """
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="please write a file")],
        tools=[
            ToolSpec(
                name="write_file",
                description="Write content to a path",
                schema_={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            ),
        ],
    )
    built = CodexMcpProvider._build_prompt(req)
    # Tool name + XML shape + schema fragment all present
    assert 'write_file' in built
    assert '<skill name="write_file">' in built
    assert "schema:" in built
    assert "please write a file" in built  # user message still concatenated


@pytest.mark.unit
def test_build_prompt_without_tools_keeps_legacy_shape():
    """Backwards-compat: when request.tools is empty, prompt is just the
    role-tagged concatenation — no synthetic tool section that would
    confuse callers expecting the old behavior."""
    req = LLMRequest(messages=[LLMMessage(role="user", content="just chat")])
    built = CodexMcpProvider._build_prompt(req)
    assert "# User" in built
    assert "just chat" in built
    assert "Available tools" not in built  # no tools section added
