"""Unit tests for kun.integration.tool_executor.

These tests verify the SkillDispatcher → ExecutorLoop.ToolExecutor bridge:

  - ok=True SkillResult → ToolResult(is_error=False, content)
  - ok=False SkillResult → ToolResult(is_error=True, content=error)
  - Unknown skill → is_error with "not registered" message
  - Dispatcher exception → is_error with str(e)
  - Non-str output is JSON-stringified
  - Multiple tool calls dispatched concurrently
  - One failing tool does not block siblings
  - tool_id propagated unchanged from ToolCall → ToolResult
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from kun.agents.executor.exec_loop import ToolCall, ToolResult
from kun.integration.tool_executor import make_tool_executor
from kun.skills.dispatcher import _REGISTRY as _SKILL_REGISTRY
from kun.skills.dispatcher import SkillResult, register


@pytest.fixture
def registered_skill_ids() -> Iterator[list[str]]:
    """Track skill_ids registered by a test and pop them after.

    Each test should append every skill_id it registers; the fixture cleans
    them up so the global ``_REGISTRY`` stays neutral between tests.
    """
    ids: list[str] = []
    yield ids
    for sid in ids:
        _SKILL_REGISTRY.pop(sid, None)


def _unique_id(prefix: str) -> str:
    """Build a per-test skill_id so concurrent tests don't collide."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---- 1. Happy path: ok skill returns content ----


@pytest.mark.asyncio
async def test_ok_skill_returns_string_content(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("echo")

    async def _echo(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=True, output=params.get("text", ""))

    register(sid, _echo)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor(
        [ToolCall(tool_id="t-1", name=sid, arguments={"text": "hello"})]
    )

    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ToolResult)
    assert r.tool_id == "t-1"
    assert r.content == "hello"
    assert r.is_error is False


# ---- 2. Unregistered skill ----


@pytest.mark.asyncio
async def test_unregistered_skill_returns_is_error() -> None:
    executor = make_tool_executor()
    results = await executor(
        [ToolCall(tool_id="t-x", name="does-not-exist-xyz", arguments={})]
    )

    assert len(results) == 1
    r = results[0]
    assert r.tool_id == "t-x"
    assert r.is_error is True
    assert "not registered" in r.content
    assert "does-not-exist-xyz" in r.content


# ---- 3. Skill returns ok=False ----


@pytest.mark.asyncio
async def test_skill_ok_false_becomes_is_error(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("fail")

    async def _fail(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=False, error="something broke")

    register(sid, _fail)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-2", name=sid, arguments={})])

    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].content == "something broke"


@pytest.mark.asyncio
async def test_skill_ok_false_with_no_error_message_uses_default(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("silentfail")

    async def _fail(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=False, error=None)

    register(sid, _fail)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-3", name=sid, arguments={})])

    assert results[0].is_error is True
    assert results[0].content == "skill failed"


# ---- 4. Dispatch raises ----


@pytest.mark.asyncio
async def test_dispatch_exception_caught(
    registered_skill_ids: list[str],
) -> None:
    """When the executor raises, dispatcher.dispatch wraps into ok=False
    with the exception text — which the adapter then surfaces as is_error.
    """
    sid = _unique_id("boom")

    async def _boom(params: dict[str, Any]) -> SkillResult:
        raise RuntimeError("kaboom inside skill")

    register(sid, _boom)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-4", name=sid, arguments={})])

    r = results[0]
    assert r.tool_id == "t-4"
    assert r.is_error is True
    # dispatcher.dispatch wraps the exception as "RuntimeError: kaboom inside skill"
    assert "kaboom inside skill" in r.content


# ---- 5. Non-str output is JSON-stringified ----


@pytest.mark.asyncio
async def test_dict_output_json_stringified(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("dict")

    async def _dict_skill(params: dict[str, Any]) -> SkillResult:
        return SkillResult(
            skill_id=sid,
            ok=True,
            output={"count": 3, "items": ["a", "b", "c"]},
        )

    register(sid, _dict_skill)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-5", name=sid, arguments={})])

    r = results[0]
    assert r.is_error is False
    # Should be valid JSON
    import json

    parsed = json.loads(r.content)
    assert parsed == {"count": 3, "items": ["a", "b", "c"]}


@pytest.mark.asyncio
async def test_list_output_json_stringified(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("list")

    async def _list_skill(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=True, output=[1, 2, 3])

    register(sid, _list_skill)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-6", name=sid, arguments={})])

    assert results[0].content == "[1, 2, 3]"


@pytest.mark.asyncio
async def test_non_json_serializable_falls_back_to_default_str(
    registered_skill_ids: list[str],
) -> None:
    """json.dumps with default=str should accept arbitrary objects."""
    sid = _unique_id("path")

    from pathlib import Path

    async def _path_skill(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=True, output={"p": Path("/tmp/x")})

    register(sid, _path_skill)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-7", name=sid, arguments={})])

    assert results[0].is_error is False
    # Path is rendered via str() because of default=str
    assert "/tmp/x" in results[0].content


# ---- 6. Multiple tools dispatched concurrently ----


@pytest.mark.asyncio
async def test_concurrent_dispatch_runs_in_parallel(
    registered_skill_ids: list[str],
) -> None:
    """Two tool calls with sleep(0.1) each should finish in ~0.1s, not 0.2s.

    Verifies asyncio.gather is actually used (not a serial for-loop).
    """
    sid_a = _unique_id("slow-a")
    sid_b = _unique_id("slow-b")

    async def _slow_a(params: dict[str, Any]) -> SkillResult:
        await asyncio.sleep(0.1)
        return SkillResult(skill_id=sid_a, ok=True, output="A done")

    async def _slow_b(params: dict[str, Any]) -> SkillResult:
        await asyncio.sleep(0.1)
        return SkillResult(skill_id=sid_b, ok=True, output="B done")

    register(sid_a, _slow_a)
    register(sid_b, _slow_b)
    registered_skill_ids.extend([sid_a, sid_b])

    executor = make_tool_executor()

    import time

    start = time.perf_counter()
    results = await executor(
        [
            ToolCall(tool_id="t-A", name=sid_a, arguments={}),
            ToolCall(tool_id="t-B", name=sid_b, arguments={}),
        ]
    )
    elapsed = time.perf_counter() - start

    # Parallel: < 0.18s (some scheduling overhead). Serial would be ≥ 0.2s.
    assert elapsed < 0.18, f"expected parallel dispatch (<0.18s), got {elapsed:.3f}s"
    assert {r.content for r in results} == {"A done", "B done"}


# ---- 7. One tool fails, others succeed ----


@pytest.mark.asyncio
async def test_one_failure_does_not_block_siblings(
    registered_skill_ids: list[str],
) -> None:
    """asyncio.gather(return_exceptions=True) isolates failures."""
    sid_ok = _unique_id("ok")
    sid_bad = _unique_id("bad")

    async def _ok_skill(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid_ok, ok=True, output="fine")

    async def _bad_skill(params: dict[str, Any]) -> SkillResult:
        raise ValueError("intentional")

    register(sid_ok, _ok_skill)
    register(sid_bad, _bad_skill)
    registered_skill_ids.extend([sid_ok, sid_bad])

    executor = make_tool_executor()
    results = await executor(
        [
            ToolCall(tool_id="t-ok", name=sid_ok, arguments={}),
            ToolCall(tool_id="t-bad", name=sid_bad, arguments={}),
            ToolCall(tool_id="t-missing", name="never-registered-xyz", arguments={}),
        ]
    )

    assert len(results) == 3

    by_tool_id = {r.tool_id: r for r in results}
    assert by_tool_id["t-ok"].is_error is False
    assert by_tool_id["t-ok"].content == "fine"

    assert by_tool_id["t-bad"].is_error is True
    assert "intentional" in by_tool_id["t-bad"].content

    assert by_tool_id["t-missing"].is_error is True
    assert "not registered" in by_tool_id["t-missing"].content


# ---- 8. tool_id propagation ----


@pytest.mark.asyncio
async def test_tool_id_propagation_preserves_call_id(
    registered_skill_ids: list[str],
) -> None:
    """ToolCall.tool_id must equal ToolResult.tool_id one-to-one in order."""
    sid = _unique_id("echo")

    async def _echo(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=True, output=params.get("i"))

    register(sid, _echo)
    registered_skill_ids.append(sid)

    call_ids = ["t-1", "t-2", "t-3", "t-4"]
    calls = [
        ToolCall(tool_id=cid, name=sid, arguments={"i": idx})
        for idx, cid in enumerate(call_ids)
    ]

    executor = make_tool_executor()
    results = await executor(calls)

    assert [r.tool_id for r in results] == call_ids
    # Each result should also carry the matching arg back as content
    assert [r.content for r in results] == ["0", "1", "2", "3"]


# ---- 9. Empty calls list ----


@pytest.mark.asyncio
async def test_empty_calls_returns_empty_list() -> None:
    executor = make_tool_executor()
    results = await executor([])
    assert results == []


# ---- 10. None output renders as empty string ----


@pytest.mark.asyncio
async def test_none_output_renders_as_empty_string(
    registered_skill_ids: list[str],
) -> None:
    sid = _unique_id("noop")

    async def _noop(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id=sid, ok=True, output=None)

    register(sid, _noop)
    registered_skill_ids.append(sid)

    executor = make_tool_executor()
    results = await executor([ToolCall(tool_id="t-9", name=sid, arguments={})])

    assert results[0].is_error is False
    assert results[0].content == ""
