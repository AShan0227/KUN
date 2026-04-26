"""Context management operators: compress, merge, and forget.

C12 keeps these as standalone deterministic operators. Claude can wire them into
chat/orchestrator later without changing their contracts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContextKind = Literal["user_msg", "assistant_msg", "tool_call", "tool_result", "summary"]

_TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)
_MIN_ITEM_TOKENS = 6


class ContextItem(BaseModel):
    """One prompt/runtime context item.

    `forgotten_ref` is populated only for tombstone summaries emitted by
    ContextForgetter. It lets the system restore the full item from an archive.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    content: str
    kind: ContextKind
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    can_forget: bool = True
    reference_count: int = Field(default=0, ge=0)
    forgotten_ref: str | None = None
    source_item_ids: list[str] = Field(default_factory=list)


class ContextCompressor:
    """Budget-aware context compressor.

    High-importance and referenced items are protected first. If the whole pack is
    still over budget, the compressor trims lower-priority items before touching
    protected items.
    """

    async def compress(
        self,
        items: list[ContextItem],
        target_tokens: int,
    ) -> list[ContextItem]:
        """Return a prompt-ready list whose estimated tokens fit target_tokens."""

        if target_tokens <= 0 or not items:
            return []

        copies = [item.model_copy(deep=True) for item in items]
        if total_tokens(copies) <= target_tokens:
            return copies

        compressed = [_compress_by_default(item) for item in copies]
        if total_tokens(compressed) <= target_tokens:
            return compressed

        return _fit_to_budget(compressed, target_tokens)


class ContextMerger:
    """Merge duplicate or near-duplicate context items without dropping content."""

    async def merge_by_topic(self, items: list[ContextItem]) -> list[ContextItem]:
        """Cluster semantically close items and merge their text losslessly."""

        clusters: list[list[ContextItem]] = []
        for item in items:
            placed = False
            for cluster in clusters:
                if _should_merge(cluster[0], item):
                    cluster.append(item)
                    placed = True
                    break
            if not placed:
                clusters.append([item])

        merged = [_merge_cluster(cluster) for cluster in clusters]
        merged.sort(key=lambda item: (item.timestamp, item.item_id))
        return merged


class ContextForgetter:
    """Fade low-value context into restoreable pointers."""

    async def forget(
        self,
        items: list[ContextItem],
        threshold: float = 0.2,
    ) -> list[ContextItem]:
        """Replace forgettable low-importance items with summary tombstones."""

        output: list[ContextItem] = []
        for item in items:
            if _can_forget(item, threshold):
                output.append(_forgotten_pointer(item))
            else:
                output.append(item.model_copy(deep=True))
        return output

    def restore(
        self,
        item: ContextItem,
        archive: dict[str, ContextItem],
    ) -> ContextItem:
        """Restore a forgotten pointer from an archive."""

        if item.forgotten_ref is None:
            return item.model_copy(deep=True)
        original = archive.get(item.forgotten_ref)
        if original is None:
            raise KeyError(f"missing forgotten context item: {item.forgotten_ref}")
        return original.model_copy(deep=True)


def estimate_tokens(text: str) -> int:
    """Small deterministic token estimate used by tests and budget gates."""

    if not text:
        return 0
    word_count = len(_TOKEN_RE.findall(text))
    # CJK / punctuation-heavy text may have few regex words, so keep a char floor.
    char_floor = max(1, len(text) // 4)
    return max(word_count, char_floor)


def total_tokens(items: list[ContextItem]) -> int:
    return sum(estimate_tokens(item.content) for item in items)


def _compress_by_default(item: ContextItem) -> ContextItem:
    if _is_protected(item):
        return item.model_copy(deep=True)

    original_tokens = estimate_tokens(item.content)
    if original_tokens <= _MIN_ITEM_TOKENS:
        return item.model_copy(deep=True)

    if item.importance >= 0.55:
        target = max(_MIN_ITEM_TOKENS, int(original_tokens * 0.65))
    elif item.importance >= 0.30:
        target = max(_MIN_ITEM_TOKENS, int(original_tokens * 0.40))
    else:
        target = max(_MIN_ITEM_TOKENS, int(original_tokens * 0.22))

    return item.model_copy(
        update={
            "content": _summarize_text(item.content, target_tokens=target),
            "kind": "summary" if item.kind != "tool_call" else item.kind,
            "source_item_ids": _source_ids(item),
        },
        deep=True,
    )


def _fit_to_budget(items: list[ContextItem], target_tokens: int) -> list[ContextItem]:
    indexed = list(enumerate(item.model_copy(deep=True) for item in items))
    indexed.sort(key=lambda pair: (_priority(pair[1]), pair[0]))

    while sum(estimate_tokens(item.content) for _, item in indexed) > target_tokens:
        changed = False
        for _, item in indexed:
            current_tokens = estimate_tokens(item.content)
            if current_tokens <= _MIN_ITEM_TOKENS:
                continue
            next_target = max(_MIN_ITEM_TOKENS, int(current_tokens * 0.72))
            next_content = _summarize_text(item.content, target_tokens=next_target)
            if next_content == item.content:
                next_content = _clip_by_chars(item.content, target_tokens=next_target)
            item.content = next_content
            item.kind = "summary" if item.kind != "tool_call" else item.kind
            item.source_item_ids = _source_ids(item)
            changed = True
            if sum(estimate_tokens(candidate.content) for _, candidate in indexed) <= target_tokens:
                break
        if not changed:
            break

    indexed.sort(key=lambda pair: pair[0])
    output = [item for _, item in indexed]
    while total_tokens(output) > target_tokens and output:
        removable = [idx for idx, item in enumerate(output) if not _is_protected(item)]
        if not removable:
            break
        output.pop(removable[-1])
    return output


def _summarize_text(text: str, *, target_tokens: int) -> str:
    words = _TOKEN_RE.findall(text)
    if len(words) <= target_tokens:
        return _clip_by_chars(text, target_tokens=target_tokens)
    head_count = max(1, target_tokens // 2)
    tail_count = max(1, target_tokens - head_count - 2)
    head = " ".join(words[:head_count])
    tail = " ".join(words[-tail_count:])
    summary = f"{head} ... {tail}"
    if estimate_tokens(summary) > target_tokens:
        return _clip_by_chars(summary, target_tokens=target_tokens)
    return summary


def _clip_by_chars(text: str, *, target_tokens: int) -> str:
    max_chars = max(8, target_tokens * 4)
    if len(text) <= max_chars:
        return text
    if max_chars <= 12:
        return text[:max_chars]
    return text[: max_chars - 4].rstrip() + " ..."


def _should_merge(left: ContextItem, right: ContextItem) -> bool:
    if left.kind != right.kind:
        return False
    if _topic_key(left.content) and _topic_key(left.content) == _topic_key(right.content):
        return True
    return _jaccard(_terms(left.content), _terms(right.content)) >= 0.45


def _merge_cluster(cluster: list[ContextItem]) -> ContextItem:
    if len(cluster) == 1:
        return cluster[0].model_copy(deep=True)

    cluster.sort(key=lambda item: (item.timestamp, item.item_id))
    first = cluster[0]
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in cluster:
        grouped[item.item_id].append(item.content)

    blocks: list[str] = []
    for item_id, contents in grouped.items():
        unique_contents = _dedupe_keep_order(contents)
        blocks.append(f"[{item_id}]\n" + "\n".join(unique_contents))

    source_ids: list[str] = []
    for item in cluster:
        source_ids.extend(_source_ids(item))

    return first.model_copy(
        update={
            "item_id": f"merged:{first.item_id}",
            "content": "\n\n".join(blocks),
            "importance": max(item.importance for item in cluster),
            "timestamp": min(item.timestamp for item in cluster),
            "can_forget": all(item.can_forget for item in cluster),
            "reference_count": sum(item.reference_count for item in cluster),
            "source_item_ids": _dedupe_keep_order(source_ids),
        },
        deep=True,
    )


def _forgotten_pointer(item: ContextItem) -> ContextItem:
    preview = _summarize_text(item.content, target_tokens=8)
    return item.model_copy(
        update={
            "content": f"[forgotten:{item.item_id}] {preview}",
            "kind": "summary",
            "forgotten_ref": item.item_id,
            "source_item_ids": _source_ids(item),
        },
        deep=True,
    )


def _can_forget(item: ContextItem, threshold: float) -> bool:
    return item.can_forget and item.reference_count == 0 and item.importance < threshold


def _is_protected(item: ContextItem) -> bool:
    return not item.can_forget or item.reference_count > 0 or item.importance >= 0.80


def _priority(item: ContextItem) -> float:
    protected_bonus = 1.0 if _is_protected(item) else 0.0
    return item.importance + protected_bonus + min(item.reference_count, 5) * 0.10


def _source_ids(item: ContextItem) -> list[str]:
    if item.source_item_ids:
        return _dedupe_keep_order(item.source_item_ids)
    if item.forgotten_ref:
        return [item.forgotten_ref]
    return [item.item_id]


def _topic_key(text: str) -> str:
    terms = sorted(_terms(text))
    if not terms:
        return ""
    return ":".join(terms[:3])


def _terms(text: str) -> set[str]:
    return {term.lower() for term in _TOKEN_RE.findall(text) if len(term) >= 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


__all__ = [
    "ContextCompressor",
    "ContextForgetter",
    "ContextItem",
    "ContextKind",
    "ContextMerger",
    "estimate_tokens",
    "total_tokens",
]
