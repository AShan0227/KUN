"""V2.3 Wire 43: Pheromone daily decay idle_batch step."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from kun.engineering.idle_batch import PheromoneDecayStep
from kun.qi.pheromone import (
    InMemoryPheromoneStorage,
    reset_pheromone_storage,
    set_pheromone_storage,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_pheromone_storage()
    yield
    reset_pheromone_storage()


@pytest.mark.asyncio
async def test_decay_step_default_decay_rate() -> None:
    storage = InMemoryPheromoneStorage()
    set_pheromone_storage(storage)
    await storage.reinforce(
        "u-test",
        source_kind="skill",
        source_id="a",
        target_kind="skill",
        target_id="b",
        relation_type="follows",
    )
    initial = storage.get_pheromone("u-test", "skill", "a", "skill", "b", "follows")
    assert initial > 0

    step = PheromoneDecayStep()
    result = await step.run("u-test")
    assert "affected" in result
    after = storage.get_pheromone("u-test", "skill", "a", "skill", "b", "follows")
    assert after < initial  # decayed


@pytest.mark.asyncio
async def test_decay_step_disabled_via_env() -> None:
    storage = InMemoryPheromoneStorage()
    set_pheromone_storage(storage)
    await storage.reinforce(
        "u-test",
        source_kind="skill",
        source_id="a",
        target_kind="skill",
        target_id="b",
        relation_type="follows",
    )
    initial = storage.get_pheromone("u-test", "skill", "a", "skill", "b", "follows")

    step = PheromoneDecayStep()
    with patch.dict(os.environ, {"KUN_PHEROMONE_DECAY_ENABLED": "0"}):
        result = await step.run("u-test")
    assert result.get("skipped") is True
    after = storage.get_pheromone("u-test", "skill", "a", "skill", "b", "follows")
    assert after == initial  # 没衰减


@pytest.mark.asyncio
async def test_decay_step_custom_rate() -> None:
    storage = InMemoryPheromoneStorage()
    set_pheromone_storage(storage)
    await storage.reinforce(
        "u-test",
        source_kind="skill",
        source_id="a",
        target_kind="skill",
        target_id="b",
        relation_type="follows",
    )

    step = PheromoneDecayStep()
    with patch.dict(os.environ, {"KUN_PHEROMONE_DECAY_RATE": "0.5"}):
        result = await step.run("u-test")
    assert result.get("decay_rate") == 0.5


@pytest.mark.asyncio
async def test_decay_step_storage_exception_doesnt_break() -> None:
    """Storage 报错 → 返回 error 字段, 不抛."""

    class BrokenStorage:
        async def decay_all(self, **kwargs):
            raise RuntimeError("DB down")

    set_pheromone_storage(BrokenStorage())

    step = PheromoneDecayStep()
    result = await step.run("u-test")
    assert result.get("affected") == 0
    assert "error" in result


def test_decay_step_registered_in_default() -> None:
    from kun.engineering.idle_batch import list_steps

    assert "pheromone_decay" in list_steps()
