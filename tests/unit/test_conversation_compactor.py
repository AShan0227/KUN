"""LT.D — ConversationCompactor 单测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.executor.compaction import (
    CompactionResult,
    ConversationCompactor,
    estimate_tokens,
)


def _msg(role: str, content: str, **extra: Any) -> dict[str, Any]:
    m = {"role": role, "content": content}
    m.update(extra)
    return m


def _long_msg(role: str, n_chars: int) -> dict[str, Any]:
    return _msg(role, "x" * n_chars)


# ---- estimate_tokens ----


def test_estimate_tokens_empty_returns_1() -> None:
    """0 ≤ floor 防止 divide by zero."""
    assert estimate_tokens([]) == 1


def test_estimate_tokens_proportional_to_chars() -> None:
    msgs = [_msg("user", "a" * 400)]
    # 400 chars + role + tiny overhead ≈ 100+ tokens (chars/4)
    tokens = estimate_tokens(msgs)
    assert tokens >= 100


def test_estimate_tokens_handles_list_content() -> None:
    """multi-modal content (list of dicts)."""
    msgs = [{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}]
    tokens = estimate_tokens(msgs)
    assert tokens >= 1


def test_estimate_tokens_handles_non_dict_silently() -> None:
    msgs = [None, "string", _msg("user", "abc")]  # type: ignore[list-item]
    tokens = estimate_tokens(msgs)
    assert tokens >= 1  # 不崩


# ---- constructor validation ----


def test_constructor_rejects_invalid_params() -> None:
    with pytest.raises(ValueError, match="token_threshold"):
        ConversationCompactor(token_threshold=0)
    with pytest.raises(ValueError, match="keep_last_k"):
        ConversationCompactor(keep_last_k=-1)
    with pytest.raises(ValueError, match="protect_first_n"):
        ConversationCompactor(protect_first_n=-1)
    with pytest.raises(ValueError, match="min_compactable"):
        ConversationCompactor(min_compactable=0)


# ---- maybe_compact: under threshold ----


@pytest.mark.asyncio
async def test_returns_none_when_under_threshold() -> None:
    compactor = ConversationCompactor(token_threshold=10_000)
    messages = [_msg("user", "hi"), _msg("assistant", "hello")]
    result = await compactor.maybe_compact(messages)
    assert result is None


# ---- maybe_compact: above threshold ----


@pytest.mark.asyncio
async def test_compacts_when_above_threshold() -> None:
    compactor = ConversationCompactor(
        token_threshold=500, keep_last_k=2, protect_first_n=1
    )
    # 1 system + 10 mid messages (each 400 chars = ~100 tokens) + 2 last
    # Total ~ 1000+ tokens
    messages = (
        [_msg("system", "anchor pinned")]
        + [_long_msg("user", 400) for _ in range(10)]
        + [_msg("user", "recent 1"), _msg("assistant", "recent 2")]
    )
    result = await compactor.maybe_compact(messages)
    assert isinstance(result, CompactionResult)
    assert result.kept_head_count == 1
    assert result.kept_tail_count == 2
    assert result.compacted_count == 10
    # compacted_messages = head(1) + summary(1) + tail(2) = 4
    assert len(result.compacted_messages) == 4
    assert result.compacted_messages[0]["role"] == "system"
    assert result.compacted_messages[1].get("_kun_compacted") is True
    assert result.compacted_messages[1]["_kun_compacted_count"] == 10


@pytest.mark.asyncio
async def test_compacted_messages_strictly_smaller_tokens() -> None:
    compactor = ConversationCompactor(token_threshold=500, keep_last_k=2)
    messages = [_long_msg("user", 500) for _ in range(15)]
    result = await compactor.maybe_compact(messages)
    assert result is not None
    assert result.compacted_tokens < result.original_tokens
    assert result.ratio < 1.0


@pytest.mark.asyncio
async def test_under_min_compactable_returns_none() -> None:
    """中间只有 1 条 → 折叠没意义."""
    compactor = ConversationCompactor(
        token_threshold=10,  # 触发阈值很低
        keep_last_k=2,
        protect_first_n=1,
        min_compactable=2,
    )
    # 1 head + 1 middle + 2 tail = 4 messages but middle=1 < min_compactable
    messages = [
        _long_msg("system", 200),
        _long_msg("user", 200),
        _long_msg("assistant", 200),
        _long_msg("user", 200),
    ]
    result = await compactor.maybe_compact(messages)
    assert result is None


@pytest.mark.asyncio
async def test_uses_injected_summarizer() -> None:
    captured: list[tuple[list[dict], dict | None]] = []

    async def my_summarizer(msgs, anchor):
        captured.append((msgs, anchor))
        return "CUSTOM SUMMARY"

    compactor = ConversationCompactor(
        token_threshold=100,
        keep_last_k=1,
        protect_first_n=1,
        summarizer=my_summarizer,
    )
    messages = (
        [_msg("system", "anchor")]
        + [_long_msg("user", 200) for _ in range(4)]
        + [_msg("user", "tail")]
    )
    result = await compactor.maybe_compact(
        messages, anchor={"goal_statement": "test goal"}
    )
    assert result is not None
    assert result.summary_text == "CUSTOM SUMMARY"
    assert result.compacted_messages[1]["content"] == "CUSTOM SUMMARY"
    # summarizer 被调用了一次
    assert len(captured) == 1
    middle_passed, anchor_passed = captured[0]
    assert len(middle_passed) == 4
    assert anchor_passed == {"goal_statement": "test goal"}


@pytest.mark.asyncio
async def test_default_summarizer_includes_anchor_recap() -> None:
    compactor = ConversationCompactor(
        token_threshold=100, keep_last_k=1, protect_first_n=1
    )
    messages = (
        [_msg("system", "anchor")]
        + [_long_msg("user", 200) for _ in range(4)]
        + [_msg("user", "tail")]
    )
    result = await compactor.maybe_compact(
        messages, anchor={"goal_statement": "implement oauth login"}
    )
    assert result is not None
    assert "implement oauth login" in result.summary_text
    assert "compacted 4 prior messages" in result.summary_text


@pytest.mark.asyncio
async def test_default_summarizer_handles_no_anchor() -> None:
    compactor = ConversationCompactor(
        token_threshold=100, keep_last_k=1, protect_first_n=1
    )
    messages = (
        [_msg("system", "x")]
        + [_long_msg("user", 200) for _ in range(4)]
        + [_msg("user", "tail")]
    )
    result = await compactor.maybe_compact(messages)
    assert result is not None
    # 无 anchor 仍能 summarize
    assert "compacted 4" in result.summary_text


@pytest.mark.asyncio
async def test_keep_last_k_zero_only_head_plus_summary() -> None:
    compactor = ConversationCompactor(
        token_threshold=100, keep_last_k=0, protect_first_n=1
    )
    messages = (
        [_msg("system", "anchor")]
        + [_long_msg("user", 200) for _ in range(5)]
    )
    result = await compactor.maybe_compact(messages)
    assert result is not None
    assert result.kept_tail_count == 0
    # head(1) + summary(1) = 2
    assert len(result.compacted_messages) == 2


@pytest.mark.asyncio
async def test_protect_first_n_zero_summarizes_from_start() -> None:
    compactor = ConversationCompactor(
        token_threshold=100, keep_last_k=2, protect_first_n=0
    )
    messages = [_long_msg("user", 200) for _ in range(10)]
    result = await compactor.maybe_compact(messages)
    assert result is not None
    assert result.kept_head_count == 0


# ---- result.ratio sanity ----


@pytest.mark.asyncio
async def test_result_ratio_in_valid_range() -> None:
    compactor = ConversationCompactor(token_threshold=200, keep_last_k=1)
    messages = [_long_msg("user", 400) for _ in range(20)]
    result = await compactor.maybe_compact(messages)
    assert result is not None
    assert 0.0 < result.ratio <= 1.0


# ---- preservation: head + tail identical (verbatim) ----


@pytest.mark.asyncio
async def test_head_and_tail_preserved_verbatim() -> None:
    compactor = ConversationCompactor(
        token_threshold=300, keep_last_k=2, protect_first_n=2
    )
    head1 = _msg("system", "anchor")
    head2 = _msg("system", "rules")
    middle = [_long_msg("user", 400) for _ in range(5)]
    tail1 = _msg("user", "TAIL ONE")
    tail2 = _msg("assistant", "TAIL TWO")
    messages = [head1, head2, *middle, tail1, tail2]

    result = await compactor.maybe_compact(messages)
    assert result is not None
    assert result.compacted_messages[0] == head1
    assert result.compacted_messages[1] == head2
    assert result.compacted_messages[-2] == tail1
    assert result.compacted_messages[-1] == tail2


@pytest.mark.asyncio
async def test_original_messages_carried_for_audit() -> None:
    """CompactionResult 保留原 messages 引用 (audit / replay)."""
    compactor = ConversationCompactor(
        token_threshold=200, keep_last_k=1, protect_first_n=1
    )
    messages = (
        [_msg("system", "anchor")]
        + [_long_msg("user", 400) for _ in range(5)]
        + [_msg("user", "tail")]
    )
    result = await compactor.maybe_compact(messages)
    assert result is not None
    # original_messages 完整保留
    assert len(result.original_messages) == 7


# ---- token_threshold property ----


def test_token_threshold_property() -> None:
    c = ConversationCompactor(token_threshold=12345)
    assert c.token_threshold == 12345
