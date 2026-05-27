"""Conversation compaction (LT.D — long context 治理).

长任务对话历史线性增长 → cost 升 + KV cache miss. 当 token 估算超阈值时,
ConversationCompactor 把中间的旧消息压缩成一个 summary message, 保留:
  - 头部 N 条 (一般是 system prompt + GoalAnchor, 不能丢)
  - 尾部 K 轮 (最近上下文, 模型当前需要的)
  - 中间历史 → injected summarizer 压缩成一条 summary message
  - 拼起来作为新的 messages list 喂下次 LLM call

设计要点:
  - token_threshold + keep_last_k + protect_first_n 都是可注入参数
  - summarizer 是 callback (DI): 真生产用 LLM, 测试用 fake
  - 估算 tokens 用 chars/4 启发式 — 真实 tokenizer 在 LT.E 接 LLM 时换
  - 返回 CompactionResult 含 original + compacted + ratio 给监控用
  - maybe_compact 在阈值以下时返回 None — caller 直接 no-op 不付 LLM call

为什么不在 LT.E (Multi-step loop) 里实装:
  - 解耦, compaction 是独立 concern, 任何 caller 都用 (Executor / Replay / Debug viewer)
  - 测试时 fake summarizer 就够, 无需 LLM
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.agents.executor.compaction")


# Summarizer: messages_to_compact + optional anchor_hint → summary text.
Summarizer = Callable[[list[dict[str, Any]], dict[str, Any] | None], Awaitable[str]]


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """启发式: 每 4 字符 ≈ 1 token (英文; 中文偏保守 4 chars ≈ 2-3 tokens).

    用 chars 估算够看趋势, 真实 tokenizer 在 LT.E 时换 (provider 提供).
    """
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # multi-modal content: list of dicts with text / image / etc
            for part in content:
                if isinstance(part, dict):
                    t = part.get("text", "")
                    if isinstance(t, str):
                        total_chars += len(t)
        role = m.get("role", "")
        if isinstance(role, str):
            total_chars += len(role) + 2  # small overhead
    return max(1, total_chars // 4)


async def _default_summarizer(
    messages: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
) -> str:
    """规则兜底 summarizer — 无 LLM 时也可用.

    把每条 message 截 60 字符后按 role 标签拼起来. 真生产用 LLM summarizer
    替换. anchor 不参与 (规则版没法用); LLM 版应当用 anchor 引导抽取.
    """
    parts: list[str] = []
    if anchor and anchor.get("goal_statement"):
        parts.append(f"[anchor recap] {anchor['goal_statement'][:120]}")
    parts.append(f"[compacted {len(messages)} prior messages]")
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " | ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        snippet = str(content).strip().replace("\n", " ")[:60]
        if snippet:
            parts.append(f"- {role}: {snippet}")
    return "\n".join(parts)


@dataclass(frozen=True)
class CompactionResult:
    """压缩结果 — 含 before/after + 统计."""

    original_messages: list[dict[str, Any]]
    compacted_messages: list[dict[str, Any]]
    """新的 messages list (head + summary + tail). 直接喂下次 LLM call."""
    summary_text: str
    kept_head_count: int  # 头部保留多少条 (一般是 system + GoalAnchor)
    kept_tail_count: int  # 尾部保留多少条
    compacted_count: int  # 折叠了多少条 (中间)
    original_tokens: int
    compacted_tokens: int
    ratio: float  # compacted_tokens / original_tokens (越小越好)


class ConversationCompactor:
    """对话历史压缩器."""

    def __init__(
        self,
        *,
        token_threshold: int = 30000,
        keep_last_k: int = 6,
        protect_first_n: int = 1,
        summarizer: Summarizer | None = None,
        min_compactable: int = 2,
    ) -> None:
        """
        Args:
          token_threshold: 触发阈值. 估算 tokens 超过此值才压缩.
          keep_last_k: 尾部完整保留多少条 (LLM 最近上下文).
          protect_first_n: 头部保留多少条 (system prompt + GoalAnchor 通常 ≥1).
          summarizer: 注入的 summarizer; None 用默认规则兜底.
          min_compactable: 中间至少要有 N 条才压缩 (1 条折成 1 条无意义).
        """
        if token_threshold <= 0:
            raise ValueError("token_threshold must be positive")
        if keep_last_k < 0:
            raise ValueError("keep_last_k must be >= 0")
        if protect_first_n < 0:
            raise ValueError("protect_first_n must be >= 0")
        if min_compactable < 1:
            raise ValueError("min_compactable must be >= 1")
        self._token_threshold = token_threshold
        self._keep_last_k = keep_last_k
        self._protect_first_n = protect_first_n
        self._summarizer = summarizer or _default_summarizer
        self._min_compactable = min_compactable

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        anchor: dict[str, Any] | None = None,
    ) -> CompactionResult | None:
        """估算 tokens; 超阈值 → 压缩; 否则 None (caller no-op).

        - 头部 protect_first_n 不动
        - 尾部 keep_last_k 不动
        - 中间 ≥ min_compactable 条 → 调 summarizer 压成 1 条 summary message
        - 若中间 < min_compactable 条 → 不压缩 (折成 1 条无意义), 返 None
        """
        original_tokens = estimate_tokens(messages)
        if original_tokens < self._token_threshold:
            return None

        head_end = min(self._protect_first_n, len(messages))
        tail_start = max(head_end, len(messages) - self._keep_last_k)
        middle = messages[head_end:tail_start]

        if len(middle) < self._min_compactable:
            log.debug(
                "compaction.below_min_compactable",
                middle_count=len(middle),
                min_compactable=self._min_compactable,
            )
            return None

        head = list(messages[:head_end])
        tail = list(messages[tail_start:])

        summary_text = await self._summarizer(middle, anchor)
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": summary_text,
            "_kun_compacted": True,
            "_kun_compacted_count": len(middle),
        }

        compacted_messages = [*head, summary_msg, *tail]
        compacted_tokens = estimate_tokens(compacted_messages)
        ratio = compacted_tokens / max(1, original_tokens)

        log.info(
            "compaction.applied",
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            compacted_count=len(middle),
            kept_head=len(head),
            kept_tail=len(tail),
            ratio=round(ratio, 3),
        )

        return CompactionResult(
            original_messages=list(messages),
            compacted_messages=compacted_messages,
            summary_text=summary_text,
            kept_head_count=len(head),
            kept_tail_count=len(tail),
            compacted_count=len(middle),
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            ratio=ratio,
        )

    @property
    def token_threshold(self) -> int:
        return self._token_threshold


__all__ = [
    "CompactionResult",
    "ConversationCompactor",
    "Summarizer",
    "estimate_tokens",
]
