"""LLMRouter → ExecutorLoop.LLMInvoker adapter.

Bridges the one-shot ``LLMRouter.invoke(LLMRequest) -> LLMResponse`` API to the
multi-step agent loop's ``LLMInvoker = Callable[[list[dict]], Awaitable[LLMStepResponse]]``
contract.

The orchestrator's ExecutorLoop keeps its conversation history as a list of
plain dicts (``{"role": ..., "content": ..., ...}``) — extra keys like
``tool_calls`` / ``_kun_compacted`` / ``is_error`` may also appear. The router,
on the other hand, demands strict ``LLMMessage`` pydantic models. This adapter
performs the lossy translation in one direction and the field rename in the
other.

Field mapping (LLMResponse → LLMStepResponse):
  - ``content`` → ``content``
  - ``tool_calls`` (``ToolCall(id, name, arguments)``) → list of
    ``exec_loop.ToolCall(tool_id=id, name, arguments)``
  - ``finish_reason`` ("stop"|"tool_use"|"length"|"error") passes through
    verbatim — exec_loop accepts any string
  - ``usage.total()`` → ``usage_tokens``
  - ``cost_usd_equivalent`` → ``cost_usd`` (canonical cross-provider cost per
    ADR-008, NOT ``cost_usd_actual`` which is provider-native pricing)
"""

from __future__ import annotations

from typing import Any

from kun.agents.executor.exec_loop import (
    LLMInvoker,
    LLMStepResponse,
)
from kun.agents.executor.exec_loop import ToolCall as ExecToolCall
from kun.interface.llm.base import (
    LLMMessage,
    LLMRequest,
    TaskProfile,
    ToolSpec,
)
from kun.interface.llm.router import LLMRouter

# Fields that LLMMessage accepts. Anything else in the input dict is dropped.
_ALLOWED_MESSAGE_FIELDS: frozenset[str] = frozenset(
    {"role", "content", "name", "tool_call_id", "cache"}
)

# Roles LLMMessage accepts (see kun.interface.llm.base.LLMRole).
_VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


def _dict_to_llm_message(raw: dict[str, Any]) -> LLMMessage:
    """Convert one ExecutorLoop-format dict into an ``LLMMessage``.

    Strips extra keys (``tool_calls``, ``is_error``, ``_kun_compacted``, etc.)
    that ExecutorLoop maintains for its own bookkeeping but the LLM-side
    pydantic model doesn't accept. Unknown roles default to ``user`` with a
    prefix so the message is still surfaced to the model — better than
    silently dropping context.
    """
    role_raw = raw.get("role", "user")
    content: str = str(raw.get("content", "") or "")

    # tool messages carry exec_loop's ``tool_id`` instead of ``tool_call_id``.
    # Lift the field rename and the error flag into a body prefix so providers
    # that flatten tool turns still see the failure signal.
    if role_raw == "tool":
        tool_call_id_raw = raw.get("tool_call_id") or raw.get("tool_id")
        tool_call_id = tool_call_id_raw if isinstance(tool_call_id_raw, str) else None
        is_error = bool(raw.get("is_error", False))
        label = "Tool error" if is_error else "Tool result"
        prefix = f"{label} [{tool_call_id}]: " if tool_call_id else f"{label}: "
        return LLMMessage(
            role="tool",
            content=f"{prefix}{content}",
            tool_call_id=tool_call_id,
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
        )

    if role_raw not in _VALID_ROLES:
        # Unknown role — coerce to user but preserve the original role in the
        # body so the model still sees the intent.
        return LLMMessage(
            role="user",
            content=f"[role={role_raw}] {content}",
        )

    # Build a filtered dict so pydantic doesn't see exec_loop-only keys
    # (``tool_calls`` on an assistant message, ``_kun_compacted`` markers, etc).
    filtered: dict[str, Any] = {
        k: v for k, v in raw.items() if k in _ALLOWED_MESSAGE_FIELDS and v is not None
    }
    filtered["role"] = role_raw
    filtered["content"] = content
    # LLMMessage types these as str | None — strip non-str leftovers.
    if "name" in filtered and not isinstance(filtered["name"], str):
        filtered.pop("name")
    if "tool_call_id" in filtered and not isinstance(filtered["tool_call_id"], str):
        filtered.pop("tool_call_id")
    return LLMMessage.model_validate(filtered)


def make_llm_invoker(
    router: LLMRouter,
    *,
    purpose: str = "execution",
    profile: TaskProfile | None = None,
    tools: list[ToolSpec] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> LLMInvoker:
    """Wrap an ``LLMRouter`` so it satisfies ExecutorLoop's ``LLMInvoker`` contract.

    Returns an ``async def(messages: list[dict[str, Any]]) -> LLMStepResponse``
    callable. Each call:

      1. Converts every input dict to an ``LLMMessage`` via
         ``LLMMessage.model_validate(...)``. Extra keys
         (``tool_calls`` / ``_kun_compacted`` / ``is_error`` / ``tool_id``)
         are filtered out; only ``role`` / ``content`` / ``name`` /
         ``tool_call_id`` / ``cache`` survive. Messages with ``role='tool'``
         keep their role since KUN's ``LLMRole`` literal already includes
         ``tool`` — but ``tool_id`` is lifted to ``tool_call_id`` and any
         ``is_error`` flag is prefixed onto the content for providers that
         flatten tool turns.
      2. Builds an ``LLMRequest(messages=..., tools=tools or [], temperature,
         max_tokens, profile=profile)``.
      3. Awaits ``router.invoke(request, purpose=purpose)``.
      4. Maps the ``LLMResponse`` back to an ``LLMStepResponse`` with the
         field rename ``ToolCall.id -> ToolCall.tool_id`` and the cost field
         choice ``cost_usd_equivalent -> cost_usd`` (ADR-008 canonical cost).

    Default ``purpose="execution"`` aligns with ADR-002 main path — Opus 4.7
    for normal multi-step agent work. Callers can pass any string; the router
    treats unknown values as ``top`` tier.

    Args:
        router: LLMRouter instance (e.g. ``get_router()``).
        purpose: Router purpose tag (``"execution"`` / ``"intent"`` / ...). See
            ``kun.interface.llm.router.TaskPurpose``.
        profile: Optional task profile; sets ``risk_level`` / ``audience`` /
            budget caps inside ``LLMRequest``.
        tools: Tool specs the LLM may emit ``tool_calls`` for. Each call sends
            the same tool list — caller is responsible for keeping it in sync
            with the actual skill registry.
        temperature: Sampling temperature.
        max_tokens: Per-call output cap.
    """
    bound_tools: list[ToolSpec] = list(tools) if tools else []

    async def _invoker(messages: list[dict[str, Any]]) -> LLMStepResponse:
        llm_messages = [_dict_to_llm_message(m) for m in messages]
        request = LLMRequest(
            messages=llm_messages,
            tools=bound_tools,
            temperature=temperature,
            max_tokens=max_tokens,
            profile=profile,
        )
        response = await router.invoke(request, purpose=purpose)  # type: ignore[arg-type]

        tool_calls = [
            ExecToolCall(
                tool_id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments),
            )
            for tc in response.tool_calls
        ]

        # LT.TOOLS-GAP fallback: some providers (e.g. codex MCP / gpt-5.5)
        # don't fill structured `tool_calls` field — they emit `<skill>` XML
        # in `content` (same protocol KUN's short-task agent_loop uses).
        # Parse them so ExecutorLoop sees tool_calls instead of an empty
        # final answer.
        if not tool_calls and response.content:
            from kun.engineering.agent_loop import parse_skill_calls

            xml_calls = parse_skill_calls(response.content)
            if xml_calls:
                tool_calls = [
                    ExecToolCall(
                        tool_id=f"xml-{i}",
                        name=call.name,
                        arguments=dict(call.params),
                    )
                    for i, call in enumerate(xml_calls)
                ]

        # If we recovered tool_calls via XML fallback, override finish_reason
        # so ExecutorLoop dispatches them instead of treating content as final.
        finish_reason = response.finish_reason
        if tool_calls and not response.tool_calls:
            finish_reason = "tool_use"

        return LLMStepResponse(
            content=response.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage_tokens=response.usage.total(),
            cost_usd=response.cost_usd_equivalent,
        )

    return _invoker


__all__ = ["make_llm_invoker"]
