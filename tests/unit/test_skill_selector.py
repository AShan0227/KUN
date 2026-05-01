"""Skill selector tests."""

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.skills.loader import SkillRegistry, parse_skill
from kun.skills.selector import SkillSelector


def _make_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(
        parse_skill(
            "---\nname: coding-pytest\ndescription: run pytest\n---\n\nbody\n",
            "a.md",
        )
    )
    reg.register(
        parse_skill(
            "---\nname: writing-markdown\ndescription: markdown writer\n---\n\nbody\n",
            "b.md",
        )
    )
    reg.register(
        parse_skill(
            "---\nname: data-csv-query\ndescription: SQL over CSV\n---\n\nbody\n",
            "c.md",
        )
    )
    reg.register(
        parse_skill(
            "---\nname: code-review\ndescription: review code safely\n---\n\nbody\n",
            "d.md",
        )
    )
    reg.register(
        parse_skill(
            "---\nname: code-propose-change\ndescription: propose guarded code patch\n---\n\nbody\n",
            "e.md",
        )
    )
    reg.register(
        parse_skill(
            "---\nname: external-skill-scout\ndescription: plan external skill search\n---\n\nbody\n",
            "f.md",
        )
    )
    return reg


def _make_task(task_type: str, required: list[str] | None = None) -> TaskRef:
    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type=task_type,
        owner=owner,
        success_criteria_short="t",
    )
    spec = None
    if required is not None:
        spec = TaskSpec(goal_detail="g", required_skills=required)
    return TaskRef(meta=meta, spec=spec)


@pytest.mark.unit
def test_explicit_required_skills_wins():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("writing.marketing", required=["data-csv-query"])
    picks = sel.select(task)
    assert picks[0].skill_id == "data-csv-query"
    assert "writing-markdown" in [p.skill_id for p in picks]


@pytest.mark.unit
def test_heuristic_substring_match():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("coding.pytest.smoke")
    picks = sel.select(task)
    assert picks and picks[0].skill_id == "coding-pytest"


@pytest.mark.unit
def test_no_match_returns_empty():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("utterly.unknown.domain")
    picks = sel.select(task)
    assert picks == []


@pytest.mark.unit
def test_summary_renders():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("coding.pytest.basic")
    picks = sel.select(task)
    summary = sel.summary(picks)
    assert "coding-pytest" in summary
    assert "run pytest" in summary


@pytest.mark.unit
def test_code_task_gets_code_capability_hints_even_without_required_skills():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("engineering.backend.bugfix")
    assert task.spec is None

    picks = sel.select(task, top_k=4)
    picked_ids = [p.skill_id for p in picks]

    assert "code-review" in picked_ids
    assert "code-propose-change" in picked_ids


@pytest.mark.unit
def test_external_skill_gap_gets_scout_hint_without_auto_installing():
    reg = _make_registry()
    sel = SkillSelector(reg)
    task = _make_task("research.capability_gap")
    task = task.model_copy(
        update={
            "spec": TaskSpec(
                goal_detail="寻找外部 skill 或工程师行为模板，但只做安全审核计划",
            )
        }
    )

    picks = sel.select(task, top_k=3)

    assert next(p.skill_id for p in picks) == "external-skill-scout"
