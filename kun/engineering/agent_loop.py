"""ReAct-style agent loop — let LLMs actually invoke skills.

Background: orchestrator's old `_execute_step` called the LLM once and
returned its answer. Skill names were hinted into the system prompt as
"available skills" but the LLM had no way to actually call them — selector
selected, and that was it.

This module wraps a multi-turn loop:

  1. Build a system prompt that lists each candidate skill with its
     input_schema and a worked example.
  2. Call the LLM.
  3. Parse the response for `<skill name="X">{json}</skill>` blocks.
  4. For each call, dispatch via ``kun.skills.dispatcher`` and capture
     the result.
  5. Append the assistant turn + a synthetic user turn carrying the
     tool results, then call the LLM again.
  6. Repeat until the LLM returns no more skill calls or we hit
     ``max_iterations``.

Why a custom protocol instead of native tool_call? The CLI providers
(Claude Code CLI, Codex MCP) are wrapped in subscription-mode and don't
expose the raw tool_call channel through their CLI surface. A textual
``<skill>`` envelope works across every provider type — including the
stub one used in tests — without requiring vendor-specific wiring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from kun.core.logging import get_logger
from kun.interface.hermes import HermesAdapter
from kun.interface.llm.base import LLMMessage, LLMRequest, LLMResponse
from kun.interface.llm.router import LLMRouter, TaskPurpose
from kun.skills.dispatcher import dispatch as skill_dispatch
from kun.skills.dispatcher import is_registered

log = get_logger("kun.agent_loop")


# ----- Skill-call envelope used between the LLM and orchestrator -----
# Example:
#   <skill name="web-search">{"query": "rust async traits"}</skill>
# Whitespace inside the JSON is allowed; multiple skill blocks per turn OK.
_CALL_RE = re.compile(
    r'<skill\s+name="([^"]+)"\s*>\s*(\{[\s\S]*?\})\s*</skill>',
    re.MULTILINE,
)


@dataclass
class SkillInvocation:
    name: str
    params: dict[str, Any]


@dataclass
class LoopStep:
    """One iteration of the agent loop."""

    iteration: int
    response: LLMResponse
    skill_calls: list[SkillInvocation] = field(default_factory=list)
    skill_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentLoopResult:
    """End-state of the loop returned to orchestrator."""

    final_answer: str
    final_response: LLMResponse  # last LLM response (carries final usage)
    iterations: list[LoopStep]
    total_cost_actual: float
    total_cost_equivalent: float
    total_input_tokens: int
    total_output_tokens: int
    pause_requests: list[dict[str, Any]] = field(default_factory=list)


def parse_skill_calls(text: str) -> list[SkillInvocation]:
    """Extract <skill name="X">{json}</skill> blocks from an LLM response."""
    out: list[SkillInvocation] = []
    for match in _CALL_RE.finditer(text):
        name = match.group(1).strip()
        body = match.group(2).strip()
        try:
            params = json.loads(body)
        except json.JSONDecodeError as e:
            log.warning("agent_loop.bad_json", skill_name=name, error=str(e))
            continue
        if not isinstance(params, dict):
            log.warning("agent_loop.params_not_object", skill_name=name)
            continue
        if not is_registered(name):
            log.info("agent_loop.unknown_skill", skill_name=name)
            continue
        out.append(SkillInvocation(name=name, params=params))
    return out


def build_skill_directive(skill_summaries: list[tuple[str, str, dict[str, Any]]]) -> str:
    """Render a ``<skill>``-aware addendum to the system prompt.

    ``skill_summaries`` is ``[(skill_id, description, input_schema), ...]``
    — pulled from selected SkillRecord candidates.
    """
    if not skill_summaries:
        return ""
    lines = [
        "你可以调用以下工具来获取信息或执行操作。需要调用时, 严格按下面的格式输出, "
        "工具调用块要单独成行, 不要嵌在叙述里。",
        "",
        "格式：",
        '<skill name="工具名">{"参数名": "参数值"}</skill>',
        "",
        "我会在工具结果回来后让你继续。如果不需要工具直接回答, 给出最终答案即可。",
        "",
        "可用工具：",
    ]
    for skill_id, desc, schema in skill_summaries:
        lines.append(f"- {skill_id}: {desc}")
        if schema:
            try:
                schema_compact = json.dumps(schema, ensure_ascii=False)
                if len(schema_compact) <= 240:
                    lines.append(f"  参数 schema: {schema_compact}")
            except (TypeError, ValueError):
                pass
    return "\n".join(lines)


def format_tool_results(results: list[dict[str, Any]]) -> str:
    """Render tool execution results as the next user-turn message.

    The LLM sees one block per tool call. We keep output short — long
    payloads (web-search large pages, big csv-query results) are
    truncated, with a note so the model knows it can ask for a re-run
    with narrower params.
    """
    parts = ["以下是工具执行结果："]
    for r in results:
        parts.append(f"\n## {r.get('skill_id')}")
        parts.append(f"- ok: {r.get('ok')}")
        if not r.get("ok"):
            parts.append(f"- error: {r.get('error')}")
        else:
            output = r.get("output")
            try:
                rendered = json.dumps(output, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered = str(output)
            if len(rendered) > 2000:
                rendered = rendered[:2000] + "\n... (truncated)"
            parts.append(f"- output:\n```json\n{rendered}\n```")
        metadata = r.get("metadata")
        if metadata:
            try:
                rendered_meta = json.dumps(metadata, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered_meta = str(metadata)
            if len(rendered_meta) > 800:
                rendered_meta = rendered_meta[:800] + "\n... (truncated)"
            parts.append(f"- metadata:\n```json\n{rendered_meta}\n```")
    parts.append("\n请基于以上结果继续。如果还需要调工具就再发一个 <skill> 块, 否则给出最终答案。")
    return "\n".join(parts)


async def run_agent_loop(
    *,
    router: LLMRouter,
    purpose: TaskPurpose,
    initial_request: LLMRequest,
    max_iterations: int = 3,
    hermes_adapter: HermesAdapter | None = None,
    hermes_context: dict[str, Any] | None = None,
) -> AgentLoopResult:
    """Drive a multi-turn ReAct conversation until the LLM stops calling skills.

    The first ``initial_request`` should already have its system prompt
    fitted with ``build_skill_directive(...)`` if the caller wants the
    LLM to consider calling skills. Otherwise this degrades to a normal
    one-shot call.
    """
    messages = list(initial_request.messages)
    iterations: list[LoopStep] = []
    last_response: LLMResponse | None = None
    total_actual = 0.0
    total_equiv = 0.0
    total_in = 0
    total_out = 0
    pause_requests: list[dict[str, Any]] = []

    for i in range(max_iterations):
        request = initial_request.model_copy(update={"messages": messages})
        response = await router.invoke(request, purpose=purpose)
        last_response = response

        total_actual += response.cost_usd_actual
        total_equiv += response.cost_usd_equivalent
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

        skill_calls = parse_skill_calls(response.content)
        step = LoopStep(iteration=i, response=response, skill_calls=skill_calls)
        iterations.append(step)

        if not skill_calls:
            # Pure answer, we're done.
            break

        # Dispatch each call and gather results
        tool_results: list[dict[str, Any]] = []
        for call in skill_calls:
            dispatch_params = call.params
            if hermes_adapter is not None:
                dispatch_params = await hermes_adapter.adapt_skill_input(
                    skill_id=call.name,
                    params=call.params,
                    context=hermes_context,
                )
            if hermes_context is not None:
                dispatch_params = {
                    **dispatch_params,
                    "_kun_context": hermes_context,
                }
            result = await skill_dispatch(call.name, dispatch_params)
            result_payload = result.model_dump(mode="json")
            if hermes_adapter is not None:
                result_payload = await hermes_adapter.adapt_skill_result(
                    skill_id=call.name,
                    result=result_payload,
                    context=hermes_context,
                )
            tool_results.append(result_payload)
        step.skill_results = tool_results

        requested_pause = [
            result
            for result in tool_results
            if isinstance(result.get("metadata"), dict)
            and result["metadata"].get("requires_task_pause") is True
        ]
        if requested_pause:
            pause_requests.extend(requested_pause)
            log.info(
                "agent_loop.pause_requested",
                skill_ids=[result.get("skill_id") for result in requested_pause],
            )
            break

        # Continue the conversation: assistant said its piece, now feed
        # the tool results back as a user-role message.
        messages.append(LLMMessage(role="assistant", content=response.content))
        messages.append(LLMMessage(role="user", content=format_tool_results(tool_results)))
    else:
        # Hit max iterations with the LLM still calling skills — surface a
        # warning but treat the latest response as the final answer.
        log.warning(
            "agent_loop.max_iterations_reached",
            max_iterations=max_iterations,
        )

    if last_response is None:
        # Shouldn't happen — max_iterations >= 1 always runs once.
        raise RuntimeError("agent loop exited without making any LLM call")

    # Strip <skill> blocks from the final answer so the user sees only prose.
    final_answer = _CALL_RE.sub("", last_response.content).strip()
    if not final_answer:
        final_answer = last_response.content.strip()

    return AgentLoopResult(
        final_answer=final_answer,
        final_response=last_response,
        iterations=iterations,
        total_cost_actual=total_actual,
        total_cost_equivalent=total_equiv,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        pause_requests=pause_requests,
    )


__all__ = [
    "AgentLoopResult",
    "LoopStep",
    "SkillInvocation",
    "build_skill_directive",
    "format_tool_results",
    "parse_skill_calls",
    "run_agent_loop",
]
