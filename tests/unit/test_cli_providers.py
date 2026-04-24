"""CLI OAuth adapter tests (ClaudeCodeProvider, CodexCliProvider).

We don't actually spawn the CLIs — we stub asyncio.create_subprocess_exec
to return canned JSON / JSONL. This verifies our parser + error handling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
from kun.interface.llm.codex_cli_provider import CodexCliProvider


@dataclass
class _FakeProc:
    returncode: int
    _stdout: bytes
    _stderr: bytes = b""

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patch asyncio.create_subprocess_exec to feed predefined outputs."""

    plan: dict[str, _FakeProc] = {}

    async def _fake(*args, **kwargs):
        # Match on the first arg (cli path) + presence of certain flags
        key = args[0]
        return plan.get(key, _FakeProc(returncode=0, _stdout=b'{"result":"?"}'))

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake)
    return plan


# =================== Claude Code CLI ===================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claude_code_parses_json_result(fake_subprocess):
    payload = {
        "type": "result",
        "is_error": False,
        "result": "answer text",
        "total_cost_usd": 0.0042,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 8,
            "cache_read_input_tokens": 5,
        },
        "modelUsage": {
            "claude-opus-4-7": {"outputTokens": 8, "costUSD": 0.0042},
        },
    }
    fake_subprocess["claude"] = _FakeProc(returncode=0, _stdout=json.dumps(payload).encode())

    p = ClaudeCodeProvider(tier="top", cli_path="claude")
    r = await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert r.content == "answer text"
    assert r.provider == "claude-code-cli"
    assert r.tier == "top"
    assert r.cost_usd_actual == 0.0  # subscription-paid
    assert abs(r.cost_usd_equivalent - 0.0042) < 1e-9
    assert r.usage.input_tokens == 10
    assert r.usage.output_tokens == 8
    assert r.usage.cached_input_tokens == 5
    assert r.model == "claude-opus-4-7"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claude_code_reports_error(fake_subprocess):
    payload = {
        "type": "result",
        "is_error": True,
        "result": "Not logged in · Please run /login",
    }
    fake_subprocess["claude"] = _FakeProc(returncode=0, _stdout=json.dumps(payload).encode())

    p = ClaudeCodeProvider(tier="top", cli_path="claude")
    with pytest.raises(RuntimeError, match="Not logged in"):
        await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claude_code_nonzero_exit(fake_subprocess):
    fake_subprocess["claude"] = _FakeProc(
        returncode=42, _stdout=b"", _stderr=b"segfault or whatever"
    )
    p = ClaudeCodeProvider(tier="top", cli_path="claude")
    with pytest.raises(RuntimeError, match="exit 42"):
        await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))


@pytest.mark.unit
def test_claude_code_split_messages():
    req = LLMRequest(
        messages=[
            LLMMessage(role="system", content="S1"),
            LLMMessage(role="user", content="U1"),
            LLMMessage(role="assistant", content="A1"),
            LLMMessage(role="user", content="U2"),
        ]
    )
    prompt, system = ClaudeCodeProvider._split_messages(req)
    assert system == "S1"
    assert "U1" in prompt and "U2" in prompt
    assert "<previous_assistant>A1</previous_assistant>" in prompt


# =================== Codex CLI ===================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_codex_parses_jsonl_stream(fake_subprocess):
    events = [
        {"type": "turn.started"},
        {"type": "agent_message", "message": "first draft"},
        {"type": "agent_message", "message": "final answer"},
        {"type": "token_count", "input_tokens": 30, "output_tokens": 12},
        {"type": "task.completed", "output": "final answer"},
    ]
    payload = b"\n".join(json.dumps(e).encode() for e in events)
    fake_subprocess["codex"] = _FakeProc(returncode=0, _stdout=payload)

    p = CodexCliProvider(tier="coding", cli_path="codex")
    r = await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert r.content == "final answer"
    assert r.usage.input_tokens == 30
    assert r.usage.output_tokens == 12
    assert r.tier == "coding"
    assert r.cost_usd_actual == 0.0
    # Cost equivalent computed from GPT-5.5 pricing (10/40 per M tok)
    expected = (30 / 1_000_000) * 10.0 + (12 / 1_000_000) * 40.0
    assert abs(r.cost_usd_equivalent - expected) < 1e-9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_codex_surfaces_auth_error(fake_subprocess):
    events = [
        {"type": "error", "message": "refresh token reused — please re-login"},
        {"type": "turn.failed", "error": {"message": "stale token"}},
    ]
    payload = b"\n".join(json.dumps(e).encode() for e in events)
    fake_subprocess["codex"] = _FakeProc(returncode=0, _stdout=payload)

    p = CodexCliProvider(tier="coding", cli_path="codex")
    with pytest.raises(RuntimeError, match="refresh token"):
        await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_codex_msg_wrapper_shape(fake_subprocess):
    # Older codex versions wrap events inside a 'msg' field
    events = [
        {"msg": {"type": "agent_message", "message": "hello from wrapper"}},
        {"msg": {"type": "token_count", "input_tokens": 5, "output_tokens": 3}},
    ]
    payload = b"\n".join(json.dumps(e).encode() for e in events)
    fake_subprocess["codex"] = _FakeProc(returncode=0, _stdout=payload)

    p = CodexCliProvider(tier="coding", cli_path="codex")
    r = await p.invoke(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))
    assert r.content == "hello from wrapper"
    assert r.usage.input_tokens == 5
    assert r.usage.output_tokens == 3


@pytest.mark.unit
def test_codex_availability_no_cli(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: None)
    assert CodexCliProvider.available() is False
