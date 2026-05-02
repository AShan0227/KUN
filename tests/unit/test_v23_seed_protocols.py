"""V2.3 seed_default_protocols — 5 starter protocols."""

from __future__ import annotations

import pytest
from kun.qi.protocol import InMemoryProtocolStorage, ProtocolRegistry
from kun.qi.seed_protocols import get_seed_protocols, seed_default_protocols


def test_get_seed_protocols_returns_5() -> None:
    protocols = get_seed_protocols()
    assert len(protocols) == 5


def test_seed_protocols_all_stable() -> None:
    for proto in get_seed_protocols():
        assert proto.status == "stable"
        assert proto.created_by == "seed"


def test_seed_protocols_unique_ids() -> None:
    ids = [p.protocol_id for p in get_seed_protocols()]
    assert len(set(ids)) == len(ids)


def test_seed_protocols_cover_main_task_types() -> None:
    patterns = {p.trigger.task_type_pattern for p in get_seed_protocols()}
    expected_prefixes = {
        "writing.creative",
        "writing.long_form",
        "coding.python",
        "decision",
        "research",
    }
    for prefix in expected_prefixes:
        assert any(prefix in pat for pat in patterns), f"{prefix} missing from seed patterns"


@pytest.mark.asyncio
async def test_seed_default_protocols_into_empty_registry() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    seeded = await seed_default_protocols(registry)
    assert seeded == 5

    listed = await registry.list_all("u-sylvan")
    assert len(listed) == 5


@pytest.mark.asyncio
async def test_seed_idempotent_no_overwrite() -> None:
    """二次 seed 跳过已存在 protocol, 返 0."""
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    first = await seed_default_protocols(registry)
    assert first == 5

    second = await seed_default_protocols(registry)
    assert second == 0  # 全已存在, 没新 seed


@pytest.mark.asyncio
async def test_find_protocol_for_writing_creative() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await seed_default_protocols(registry)

    found = await registry.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.3, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is not None
    assert found.protocol_id == "writing.creative.short"
    assert found.execution.mode == "SMART"


@pytest.mark.asyncio
async def test_find_protocol_for_coding_python() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await seed_default_protocols(registry)

    found = await registry.find_protocol_for(
        {"task_type": "coding.python.fastapi", "complexity_score": 0.7, "risk_level": "high"},
        "u-sylvan",
    )
    assert found is not None
    assert found.protocol_id == "coding.python.fastapi"
    assert found.execution.mode == "MAX"


@pytest.mark.asyncio
async def test_find_protocol_for_unknown_task_returns_none() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await seed_default_protocols(registry)

    found = await registry.find_protocol_for(
        {"task_type": "weird.unknown.thing", "complexity_score": 0.5, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is None
