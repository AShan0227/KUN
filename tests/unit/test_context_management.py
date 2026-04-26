"""C12 context management operators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.context.management import (
    ContextCompressor,
    ContextForgetter,
    ContextItem,
    ContextKind,
    ContextMerger,
    estimate_tokens,
    total_tokens,
)


def _item(
    item_id: str,
    content: str,
    *,
    kind: ContextKind = "assistant_msg",
    importance: float = 0.5,
    can_forget: bool = True,
    reference_count: int = 0,
    minutes: int = 0,
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        content=content,
        kind=kind,
        importance=importance,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes),
        can_forget=can_forget,
        reference_count=reference_count,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compressor_keeps_items_when_under_budget() -> None:
    items = [_item("a", "short context", importance=0.1)]

    result = await ContextCompressor().compress(items, target_tokens=50)

    assert result == items
    assert result is not items


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compressor_respects_target_budget() -> None:
    items = [
        _item("hi", " ".join(f"important{i}" for i in range(80)), importance=0.95),
        _item("lo", " ".join(f"noise{i}" for i in range(120)), importance=0.1),
    ]

    result = await ContextCompressor().compress(items, target_tokens=70)

    assert total_tokens(result) <= 70
    assert any(item.item_id == "hi" for item in result)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compressor_preserves_referenced_item_before_low_value_item() -> None:
    items = [
        _item("ref", " ".join(f"keep{i}" for i in range(60)), importance=0.45, reference_count=2),
        _item("low", " ".join(f"drop{i}" for i in range(60)), importance=0.1),
    ]

    result = await ContextCompressor().compress(items, target_tokens=45)

    ids = [item.item_id for item in result]
    assert "ref" in ids
    assert all(item.reference_count > 0 or item.item_id != "ref" for item in result)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compressor_marks_summaries_with_source_ids() -> None:
    item = _item("a", " ".join(f"token{i}" for i in range(100)), importance=0.3)

    result = await ContextCompressor().compress([item], target_tokens=20)

    assert result[0].kind == "summary"
    assert result[0].source_item_ids == ["a"]
    assert "..." in result[0].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merger_merges_similar_same_kind_without_losing_content() -> None:
    items = [
        _item("a", "pytest failure retry minimal fix regression", kind="tool_result"),
        _item("b", "pytest failure retry minimal fix report", kind="tool_result", minutes=1),
        _item("c", "sales lead call script", kind="tool_result", minutes=2),
    ]

    result = await ContextMerger().merge_by_topic(items)

    merged = next(item for item in result if item.item_id == "merged:a")
    assert "pytest failure retry minimal fix regression" in merged.content
    assert "pytest failure retry minimal fix report" in merged.content
    assert merged.source_item_ids == ["a", "b"]
    assert any(item.item_id == "c" for item in result)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merger_does_not_merge_different_kinds() -> None:
    items = [
        _item("a", "same topic same words", kind="tool_call"),
        _item("b", "same topic same words", kind="tool_result"),
    ]

    result = await ContextMerger().merge_by_topic(items)

    assert [item.item_id for item in result] == ["a", "b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merger_keeps_highest_importance_and_reference_count() -> None:
    items = [
        _item("a", "tenant rls migration policy", importance=0.2, reference_count=1),
        _item("b", "tenant rls migration constraint", importance=0.9, reference_count=3),
    ]

    result = await ContextMerger().merge_by_topic(items)

    assert len(result) == 1
    assert result[0].importance == 0.9
    assert result[0].reference_count == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgetter_replaces_low_value_item_with_pointer() -> None:
    item = _item("low", "temporary scratch note with little future value", importance=0.05)

    result = await ContextForgetter().forget([item], threshold=0.2)

    assert result[0].kind == "summary"
    assert result[0].forgotten_ref == "low"
    assert "[forgotten:low]" in result[0].content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forgetter_keeps_referenced_or_non_forgettable_items() -> None:
    items = [
        _item("ref", "referenced output", importance=0.01, reference_count=1),
        _item("fixed", "permanent rule", importance=0.01, can_forget=False),
    ]

    result = await ContextForgetter().forget(items, threshold=0.2)

    assert [item.forgotten_ref for item in result] == [None, None]
    assert [item.content for item in result] == ["referenced output", "permanent rule"]


@pytest.mark.unit
def test_forgetter_restores_pointer_from_archive() -> None:
    original = _item("low", "full original content", importance=0.05)
    pointer = ContextItem(
        item_id="low",
        content="[forgotten:low] full original",
        kind="summary",
        forgotten_ref="low",
    )

    restored = ContextForgetter().restore(pointer, {"low": original})

    assert restored == original


@pytest.mark.unit
def test_forgetter_restore_missing_pointer_raises() -> None:
    pointer = ContextItem(
        item_id="low",
        content="[forgotten:low] full original",
        kind="summary",
        forgotten_ref="low",
    )

    with pytest.raises(KeyError, match="missing forgotten context item"):
        ContextForgetter().restore(pointer, {})


@pytest.mark.unit
def test_estimate_tokens_handles_cjk_text() -> None:
    assert estimate_tokens("中文上下文压缩测试") > 0
