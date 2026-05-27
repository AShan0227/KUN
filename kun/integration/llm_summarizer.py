"""LLMRouter → ConversationCompactor.Summarizer adapter.

`kun.agents.executor.compaction.ConversationCompactor` takes a ``Summarizer``
callback to fold mid-conversation history into a single condensed message. The
default summarizer in that module is rule-based (concatenates truncated message
previews) — useful for tests and as a fail-safe, but not what production wants.

This adapter wraps an :class:`LLMRouter` so the compactor calls a real model
instead. Returned summarizer:

  * builds a Chinese system prompt that orders the model to compress prior
    conversation into ≤ ``max_summary_tokens`` characters while preserving
    decisions / tool results / explicit instructions
  * if a goal anchor is provided, prepends a ``=== GOAL ANCHOR ===`` block so
    the model knows which threads to keep
  * serializes ``messages_to_compact`` as JSON on the user turn (after a "请
    压缩以下 N 条消息:" header)
  * routes through ``router.invoke(request, purpose=...)`` — the default
    ``"summarization"`` purpose maps to the cheap tier (router treats unknown
    purposes as ``top``; pick whichever is configured for compression)
  * returns ``response.content`` verbatim as the summary string
  * on **any** exception from the router (network / quota / provider failure)
    catches it, logs a warning, and falls back to a rule-based mini-summary so
    callers never see ``None`` or a raised error — the ``Summarizer`` signature
    promises a ``str``.

The fallback intentionally mirrors :func:`_default_summarizer` from
``kun.agents.executor.compaction`` (truncate-and-tag) but is reproduced inline
so this module stays decoupled from compaction internals.
"""

from __future__ import annotations

import json
from typing import Any

from kun.agents.executor.compaction import Summarizer
from kun.core.logging import get_logger
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.router import LLMRouter

log = get_logger("kun.integration.llm_summarizer")


_SYSTEM_PROMPT_TEMPLATE = """你是 KUN 的对话压缩器. 用户会给你一段长对话历史. 你需要:

1. 输出一个紧凑的摘要 (≤ {budget} 字符), 让模型读了之后仍能理解上下文.
2. 保留所有关键决策 / 关键工具调用结果 / 用户明确指令.
3. 去掉寒暄 / 重复 / 失败的尝试细节.
4. 若提供 GOAL ANCHOR, 用 anchor 指导你保留什么 (与 anchor 相关的细节优先).

只输出摘要文本, 不要 markdown 标题 / 不要前后缀."""


def _format_anchor_block(anchor: dict[str, Any]) -> str:
    """Render a ``=== GOAL ANCHOR ===`` block prepended onto the system prompt."""
    goal = str(anchor.get("goal_statement", ""))
    success_criteria = anchor.get("success_criteria", [])
    return f"=== GOAL ANCHOR ===\n{goal}\nSuccess criteria: {success_criteria}\n===\n\n"


def _build_system_prompt(
    *,
    max_summary_tokens: int,
    anchor: dict[str, Any] | None,
) -> str:
    base = _SYSTEM_PROMPT_TEMPLATE.format(budget=max_summary_tokens)
    if anchor:
        return _format_anchor_block(anchor) + base
    return base


def _serialize_messages_for_user_turn(messages: list[dict[str, Any]]) -> str:
    """Turn the to-be-compacted messages into a JSON blob for the user turn.

    Each entry is reduced to ``{"role": str, "content": str}``; multimodal
    list content is flattened to text. JSON is rendered with
    ``ensure_ascii=False`` so Chinese tokens stay human-readable in traces.
    """
    serializable: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        content = m.get("content", "")
        if isinstance(content, list):
            content = " | ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        serializable.append({"role": role, "content": str(content)})

    body = json.dumps(serializable, ensure_ascii=False)
    return f"请压缩以下 {len(serializable)} 条消息:\n{body}"


def _rule_based_fallback(
    messages: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
) -> str:
    """Mirror of ``compaction._default_summarizer`` — used when the LLM fails.

    Reproduced inline so this adapter doesn't reach into the compactor's
    private helpers. Output shape kept similar so logs / replay tooling can
    detect "this came from fallback" by the leading ``[fallback summary]`` tag.
    """
    parts: list[str] = ["[fallback summary]"]
    if anchor and anchor.get("goal_statement"):
        parts.append(f"[anchor recap] {str(anchor['goal_statement'])[:120]}")
    parts.append(f"[compacted {len(messages)} prior messages]")
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " | ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        snippet = str(content).strip().replace("\n", " ")[:60]
        if snippet:
            parts.append(f"- {role}: {snippet}")
    return "\n".join(parts)


def make_llm_summarizer(
    router: LLMRouter,
    *,
    purpose: str = "summarization",
    max_summary_tokens: int = 600,
    temperature: float = 0.2,
) -> Summarizer:
    """Wrap ``LLMRouter`` into a :data:`Summarizer` for ``ConversationCompactor``.

    Returned async callable:

    Args:
        messages_to_compact: middle slice of the conversation that the
            compactor wants folded into one message.
        anchor: optional ``GoalAnchor``-shaped dict (``goal_statement`` +
            ``success_criteria``). When present, prepended to the system prompt
            so the model preserves anchor-relevant detail preferentially.

    Returns:
        Summary text (string). Never raises — on router/provider failure the
        adapter logs a warning and returns a rule-based mini-summary so the
        compactor's signature contract (``Awaitable[str]``) holds.

    Args (configuration):
        router: LLMRouter instance (e.g. ``get_router()``).
        purpose: Router purpose tag. Default ``"summarization"`` is intended
            for the cheap tier (compression is a low-stakes job). Unknown
            purposes fall back to the router's ``top`` mapping — pick a value
            that's mapped explicitly in production (``"compression"`` is the
            currently registered low-tier purpose in ``router.TaskPurpose``).
        max_summary_tokens: Output cap forwarded to ``LLMRequest.max_tokens``
            and also baked into the system prompt as the char budget.
        temperature: Sampling temperature; default ``0.2`` keeps summaries
            deterministic.
    """

    async def _summarize(
        messages_to_compact: list[dict[str, Any]],
        anchor: dict[str, Any] | None,
    ) -> str:
        system_prompt = _build_system_prompt(
            max_summary_tokens=max_summary_tokens,
            anchor=anchor,
        )
        user_content = _serialize_messages_for_user_turn(messages_to_compact)
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=temperature,
            max_tokens=max_summary_tokens,
        )
        try:
            response = await router.invoke(request, purpose=purpose)  # type: ignore[arg-type]
        except Exception as e:
            log.warning(
                "llm_summarizer.router_failed",
                error=str(e),
                error_type=type(e).__name__,
                message_count=len(messages_to_compact),
                purpose=purpose,
                fallback="rule_based",
            )
            return _rule_based_fallback(messages_to_compact, anchor)

        content = response.content or ""
        if not content.strip():
            # Empty/whitespace-only responses still need *something* downstream;
            # fall back rather than poison the conversation with a blank turn.
            log.warning(
                "llm_summarizer.empty_response",
                message_count=len(messages_to_compact),
                purpose=purpose,
                fallback="rule_based",
            )
            return _rule_based_fallback(messages_to_compact, anchor)
        return content

    return _summarize


__all__ = ["make_llm_summarizer"]
