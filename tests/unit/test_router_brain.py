"""TaskRouter (brain) tests."""

import pytest
from kun.brain.router import TaskRouter
from kun.datamodel.task import Owner, TaskMeta


def _mk_meta(task_type: str, risk: str = "low", complexity: float = 0.3) -> TaskMeta:
    owner = Owner(tenant_id="u-sylvan")
    return TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type=task_type,
        risk_level=risk,
        complexity_score=complexity,
        owner=owner,
        success_criteria_short="t",
    )


@pytest.mark.unit
def test_coding_task_routes_to_coder():
    r = TaskRouter()
    choice = r.choose(_mk_meta("coding.python.fastapi"))
    assert choice.role_template_id == "rt-coder"
    assert choice.purpose == "coding"
    assert choice.task_profile.needs_coding is True


@pytest.mark.unit
def test_writing_task_routes_to_writer():
    r = TaskRouter()
    choice = r.choose(_mk_meta("writing.marketing"))
    assert choice.role_template_id == "rt-writer"
    assert choice.task_profile.needs_creative is True


@pytest.mark.unit
def test_research_task():
    r = TaskRouter()
    choice = r.choose(_mk_meta("research.trend_scan"))
    assert choice.role_template_id == "rt-researcher"


@pytest.mark.unit
def test_default_fallback():
    r = TaskRouter()
    choice = r.choose(_mk_meta("unknown.type"))
    assert choice.role_template_id == "rt-default"


@pytest.mark.unit
def test_high_complexity_enables_reasoning():
    r = TaskRouter()
    choice = r.choose(_mk_meta("research.foo", complexity=0.8))
    assert choice.task_profile.needs_reasoning is True
