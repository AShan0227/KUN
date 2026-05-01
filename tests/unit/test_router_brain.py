"""TaskRouter (brain) tests."""

import pytest
from kun.brain.router import TaskRouter
from kun.datamodel.capability import (
    Boundaries,
    Capability,
    DecayModel,
    QualityMetrics,
    Stats,
)
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


class _CapabilityCache:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    async def best_capability(
        self,
        *,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        task_type: str,
    ) -> Capability | None:
        assert tenant_id == "tenant-a"
        assert entity_type == "role_template"
        score = self.scores.get(entity_id)
        if score is None:
            return None
        stats = Stats(total_invocations=20)
        stats.success_rate = score
        return Capability(
            task_type=task_type,
            stats=stats,
            quality=QualityMetrics(
                avg_rubric_score=score * 5,
                consistency_score=score,
            ),
            decay=DecayModel(effective_sample_size=20),
            boundaries=Boundaries(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_card_can_override_role_template_when_clearly_better():
    router = TaskRouter()
    choice = await router.choose_with_capability(
        _mk_meta("writing.marketing"),
        tenant_id="tenant-a",
        capability_cache=_CapabilityCache({"rt-writer": 0.45, "rt-researcher": 0.9}),
    )

    assert choice.role_template_id == "rt-researcher"
    assert choice.route_reason.startswith("capability_card_override:")
    assert choice.capability_scores["rt-researcher"] > choice.capability_scores["rt-writer"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_card_does_not_override_small_margin():
    router = TaskRouter()
    choice = await router.choose_with_capability(
        _mk_meta("writing.marketing"),
        tenant_id="tenant-a",
        capability_cache=_CapabilityCache({"rt-writer": 0.72, "rt-researcher": 0.78}),
    )

    assert choice.role_template_id == "rt-writer"
    assert choice.route_reason == "rule_match_with_capability_scores"
