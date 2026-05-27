"""SkillDispatcher → ExecutorLoop.ToolExecutor adapter.

ExecutorLoop hands a list of ``ToolCall(tool_id, name, arguments)`` to a
``ToolExecutor`` callback and expects a parallel list of ``ToolResult`` back.
This adapter routes each call through ``kun.skills.dispatcher.dispatch`` —
the LLM was given the skill registry as its tool list, so ``tool.name``
*is* the ``skill_id``.

Concurrency: all calls in one batch are awaited via ``asyncio.gather`` with
``return_exceptions=True`` so a single misbehaving skill cannot block its
siblings (the loop only counts the batch as a failure when **every** sibling
errors). Any exception is converted to ``ToolResult(is_error=True)`` so the
loop sees a uniform shape.

Output marshalling:
  - String outputs pass through verbatim.
  - Non-str outputs are JSON-stringified with ``ensure_ascii=False`` and
    ``default=str`` (so datetime / dataclasses / Path round-trip without
    raising).
  - ``SkillResult.ok=False`` becomes ``is_error=True`` with the dispatcher's
    ``error`` field as content (or ``"skill failed"`` if missing).
  - ``is_registered`` is checked upfront so unknown skills short-circuit
    *before* hitting the dispatcher (which would itself return ok=False with
    the same message — the explicit check makes the adapter's contract
    obvious).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from kun.agents.executor.exec_loop import ToolCall, ToolExecutor, ToolResult
from kun.core.logging import get_logger
from kun.skills.dispatcher import SkillResult, dispatch, is_registered

log = get_logger("kun.integration.tool_executor")


def _stringify_output(output: Any) -> str:
    """Render skill output as a string the LLM can read back as tool content."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        # Fallback: repr() is always safe and at least conveys structure.
        log.warning(
            "tool_executor.json_dump_failed",
            output_type=type(output).__name__,
            error=str(e),
        )
        return repr(output)


def _result_from_skill(call: ToolCall, sk: SkillResult) -> ToolResult:
    """Convert ``SkillResult`` → ``ToolResult`` preserving ``tool_id``."""
    if sk.ok:
        return ToolResult(
            tool_id=call.tool_id,
            content=_stringify_output(sk.output),
            is_error=False,
        )
    return ToolResult(
        tool_id=call.tool_id,
        content=sk.error or "skill failed",
        is_error=True,
    )


async def _dispatch_one(call: ToolCall) -> ToolResult:
    """Run one tool call. Always returns a ToolResult — never raises.

    Unknown skill / dispatcher exception → ``ToolResult(is_error=True)`` with
    a human-readable content explaining what went wrong.
    """
    if not is_registered(call.name):
        return ToolResult(
            tool_id=call.tool_id,
            content=f"skill not registered: {call.name}",
            is_error=True,
        )
    try:
        sk = await dispatch(call.name, call.arguments)
    except Exception as e:
        log.warning(
            "tool_executor.dispatch_raised",
            skill_id=call.name,
            tool_id=call.tool_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return ToolResult(
            tool_id=call.tool_id,
            content=str(e) if str(e) else type(e).__name__,
            is_error=True,
        )
    return _result_from_skill(call, sk)


def make_tool_executor() -> ToolExecutor:
    """Wrap ``kun.skills.dispatcher`` into ExecutorLoop's ToolExecutor contract.

    For each ``ToolCall(tool_id, name, arguments)`` in the input list:

      - ``name`` is treated as the ``skill_id`` (the LLM was told the skill
        registry as its tools).
      - If the skill is not registered →
        ``ToolResult(tool_id, content="skill not registered: {name}", is_error=True)``.
      - Otherwise → ``await dispatch(name, arguments)``; convert ``SkillResult``:
          * ``ok=True``  → ``ToolResult(content=stringified(output), is_error=False)``
          * ``ok=False`` → ``ToolResult(content=error or "skill failed", is_error=True)``
      - Any exception during dispatch →
        ``ToolResult(content=str(e), is_error=True)``.

    All calls in the batch are dispatched concurrently via ``asyncio.gather``
    with ``return_exceptions=True`` so a single bad tool can't block siblings.
    """

    async def _executor(calls: list[ToolCall]) -> list[ToolResult]:
        if not calls:
            return []
        gathered = await asyncio.gather(
            *(_dispatch_one(c) for c in calls),
            return_exceptions=True,
        )
        # _dispatch_one already catches; this is belt-and-suspenders for
        # asyncio internals (CancelledError etc) so the list shape is stable.
        results: list[ToolResult] = []
        for call, item in zip(calls, gathered, strict=True):
            if isinstance(item, ToolResult):
                results.append(item)
            elif isinstance(item, BaseException):
                log.warning(
                    "tool_executor.gather_unexpected",
                    skill_id=call.name,
                    tool_id=call.tool_id,
                    error=str(item),
                    error_type=type(item).__name__,
                )
                results.append(
                    ToolResult(
                        tool_id=call.tool_id,
                        content=str(item) if str(item) else type(item).__name__,
                        is_error=True,
                    )
                )
            else:
                # Unreachable — gather only returns the awaitable result or an
                # exception. Guard for type safety.
                results.append(
                    ToolResult(
                        tool_id=call.tool_id,
                        content=f"unexpected dispatch return type: {type(item).__name__}",
                        is_error=True,
                    )
                )
        return results

    return _executor


__all__ = ["make_tool_executor"]
