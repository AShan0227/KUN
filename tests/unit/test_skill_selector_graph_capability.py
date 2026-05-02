from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.context.graph_traversal import NeighborEntity
from kun.datamodel.task import Owner, TaskMeta, TaskRef
from kun.skills.loader import SkillRegistry, parse_skill
from kun.skills.selector import SkillSelector


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    for name, desc in [
        ("coding-pytest", "run pytest"),
        ("debugger", "debug code"),
        ("writer", "write text"),
    ]:
        reg.register(parse_skill(f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", name))
    return reg


def _task() -> TaskRef:
    owner = Owner(tenant_id="tenant-a")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type="coding.pytest",
        owner=owner,
        success_criteria_short="run tests",
    )
    return TaskRef(meta=meta)


class _Graph:
    async def neighbors(self, *, kind: str, entity_id: str, hops: int):
        assert kind == "skill"
        assert entity_id == "coding-pytest"
        assert hops == 1
        return [
            NeighborEntity(
                entity_kind="skill",
                entity_id="debugger",
                relation_type="co_occurs",
                confidence=0.9,
                hops=1,
                via_path=(("skill", "coding-pytest"), ("skill", "debugger")),
                pheromone_strength=0.4,
            )
        ]


class _Cap:
    def capability_score(self):
        return SimpleNamespace(value=0.9)


class _Cache:
    async def best_capability(self, **kwargs):
        if kwargs["entity_id"] == "debugger":
            return _Cap()
        return None


@pytest.mark.asyncio
async def test_skill_selector_expands_graph_and_boosts_capability() -> None:
    selector = SkillSelector(
        _registry(),
        capability_cache=_Cache(),  # type: ignore[arg-type]
        graph_traversal=_Graph(),
    )

    picks = await selector.select_with_graph_and_capability(
        _task(),
        top_k=2,
        tenant_id="tenant-a",
    )

    assert [p.skill_id for p in picks] == ["debugger", "coding-pytest"]
