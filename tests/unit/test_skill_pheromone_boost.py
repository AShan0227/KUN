"""V2.3 Wire 47: Skill selector + Pheromone 联动测试."""

from __future__ import annotations

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef
from kun.engineering.credit_assignment import get_contribution_tracker
from kun.qi.pheromone import (
    InMemoryPheromoneStorage,
    reset_pheromone_storage,
    set_pheromone_storage,
)
from kun.skills.loader import SkillManifest, SkillRecord, SkillRegistry
from kun.skills.selector import SkillSelector


@pytest.fixture(autouse=True)
def _isolate():
    reset_pheromone_storage()
    get_contribution_tracker().reset()
    yield
    reset_pheromone_storage()
    get_contribution_tracker().reset()


def _make_skill(skill_id: str, description: str = "test skill") -> SkillRecord:
    return SkillRecord(
        skill_id=skill_id,
        manifest=SkillManifest(
            name=skill_id,
            version="1.0.0",
            description=description,
            license="Apache-2.0",
        ),
        body_md="dummy body",
        spdx_license="Apache-2.0",
        source_path="/tmp/dummy",
    )


def _task(task_type: str = "writing.creative") -> TaskRef:
    owner = Owner(tenant_id="u-test")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("test", owner),
            task_type=task_type,
            owner=owner,
            success_criteria_short="test",
        )
    )


def test_select_without_pheromone_uses_overlap() -> None:
    """无 prior_skill → 现行 heuristic overlap 行为."""
    reg = SkillRegistry()
    reg.register(_make_skill("writing-polish"))
    reg.register(_make_skill("coding-review"))
    selector = SkillSelector(reg)

    skills = selector.select(_task("writing.creative"), top_k=3)
    skill_ids = [s.skill_id for s in skills]
    assert "writing-polish" in skill_ids
    # coding-review 没匹配 task_type → 不返


def test_select_with_pheromone_boosts_strong_chain() -> None:
    """有 prior_skill + pheromone 强 → 该 skill 排前."""
    reg = SkillRegistry()
    reg.register(_make_skill("writing-polish"))
    reg.register(_make_skill("writing-review"))
    selector = SkillSelector(reg)

    storage = InMemoryPheromoneStorage()
    set_pheromone_storage(storage)

    # 模拟"reader → writing-review" 链路被走过 10 次
    import asyncio

    async def reinforce():
        for _ in range(10):
            await storage.reinforce(
                "u-test",
                source_kind="skill",
                source_id="reader",
                target_kind="skill",
                target_id="writing-review",
                relation_type="follows",
            )

    asyncio.run(reinforce())

    skills = selector.select(_task("writing.creative"), prior_skill="reader", top_k=2)
    # writing-review 应该排前 (pheromone 加成)
    assert skills[0].skill_id == "writing-review"


def test_select_no_prior_skill_no_pheromone_query() -> None:
    """prior_skill=None → 不查 pheromone (向后兼容)."""
    reg = SkillRegistry()
    reg.register(_make_skill("writing-polish"))
    selector = SkillSelector(reg)

    skills = selector.select(_task("writing.creative"))  # 没 prior_skill
    assert len(skills) == 1
    assert skills[0].skill_id == "writing-polish"


def test_select_pheromone_storage_failure_falls_back() -> None:
    """pheromone storage 抛异常 → 静默回退 overlap 行为."""
    reg = SkillRegistry()
    reg.register(_make_skill("writing-polish"))
    selector = SkillSelector(reg)

    class _CrashStorage:
        def get_pheromone(self, *a, **kw):
            raise RuntimeError("crash")

    set_pheromone_storage(_CrashStorage())  # type: ignore[arg-type]

    skills = selector.select(_task("writing.creative"), prior_skill="reader", top_k=2)
    # 不抛, 仍返
    assert len(skills) == 1
    assert skills[0].skill_id == "writing-polish"


def test_select_pheromone_doesnt_break_required_skills() -> None:
    """required_skills 是强信号，但不再绕过候选扩展。"""
    from kun.datamodel.task import TaskSpec

    reg = SkillRegistry()
    reg.register(_make_skill("must-use-skill"))
    reg.register(_make_skill("writing-polish"))
    selector = SkillSelector(reg)

    owner = Owner(tenant_id="u-test")
    task = TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("test", owner),
            task_type="writing.creative",
            owner=owner,
            success_criteria_short="test",
        ),
        spec=TaskSpec(
            goal_detail="x",
            required_skills=["must-use-skill"],
        ),
    )

    skills = selector.select(task, prior_skill="reader")
    assert skills[0].skill_id == "must-use-skill"
    assert "writing-polish" in [skill.skill_id for skill in skills]


def test_select_uses_moe_credit_inside_relevant_candidates() -> None:
    """历史贡献高的同类 skill 会前排，但不会跨任务类型乱抢。"""
    reg = SkillRegistry()
    reg.register(_make_skill("writing-polish"))
    reg.register(_make_skill("writing-review"))
    reg.register(_make_skill("coding-review"))
    selector = SkillSelector(reg)

    tracker = get_contribution_tracker()
    tracker.seed_counts(
        "skill:writing-review",
        used_count=10,
        pass_count=10,
        critical_count=10,
        tenant_id="u-test",
    )
    tracker.seed_counts(
        "skill:coding-review",
        used_count=20,
        pass_count=20,
        critical_count=20,
        tenant_id="u-test",
    )

    skills = selector.select(_task("writing.creative"), top_k=3)

    assert [skill.skill_id for skill in skills] == ["writing-review", "writing-polish"]
