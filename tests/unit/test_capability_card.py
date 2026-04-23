"""Capability card tests."""

import pytest
from kun.datamodel.capability import (
    Boundaries,
    Capability,
    CapabilityCard,
    DecayModel,
    EntityRef,
    QualityMetrics,
    Stats,
)


def _mk_cap(task_type: str, successes: int, trials: int, rubric: float) -> Capability:
    stats = Stats(
        total_invocations=trials,
        success_count=successes,
        failure_count=trials - successes,
        avg_cost_usd=0.05,
        avg_duration_sec=20,
    )
    stats.recompute_rate()
    return Capability(
        task_type=task_type,
        stats=stats,
        quality=QualityMetrics(avg_rubric_score=rubric, consistency_score=0.8),
        decay=DecayModel(half_life_days=30, effective_sample_size=trials),
        boundaries=Boundaries(),
    )


@pytest.mark.unit
def test_find_best_match_walks_hierarchy():
    card = CapabilityCard(
        entity_ref=EntityRef(entity_type="role_template", entity_id="rt-coder"),
        capabilities=[
            _mk_cap("coding.python", 45, 50, 4.2),
            _mk_cap("coding", 30, 40, 3.8),
        ],
    )
    assert card.find_best_match("coding.python.fastapi").task_type == "coding.python"
    assert card.find_best_match("coding.javascript").task_type == "coding"
    assert card.find_best_match("nonexistent.type") is None


@pytest.mark.unit
def test_recompute_summary_marks_maturity():
    # Small sample → cold_start
    card = CapabilityCard(
        entity_ref=EntityRef(entity_type="model", entity_id="claude-opus-4-7"),
        capabilities=[_mk_cap("coding.python", 9, 10, 4.5)],
    )
    card.capabilities[0].decay.effective_sample_size = 10
    card.recompute_summary()
    assert card.maturity == "cold_start"
    assert card.primary_strength == "coding.python"

    # Medium sample → warming_up
    card.capabilities[0].decay.effective_sample_size = 75
    card.recompute_summary()
    assert card.maturity == "warming_up"

    # Large sample → mature
    card.capabilities.append(_mk_cap("coding.javascript", 190, 200, 4.0))
    card.capabilities[0].decay.effective_sample_size = 100
    card.capabilities[1].decay.effective_sample_size = 200
    card.recompute_summary()
    assert card.maturity == "mature"


@pytest.mark.unit
def test_duplicate_task_type_rejected():
    with pytest.raises(Exception):
        CapabilityCard(
            entity_ref=EntityRef(entity_type="role_template", entity_id="x"),
            capabilities=[
                _mk_cap("coding.python", 10, 10, 4),
                _mk_cap("coding.python", 5, 5, 3),
            ],
        )


@pytest.mark.unit
def test_capability_score_composed():
    cap = _mk_cap("coding.python", 45, 50, 4.5)
    sc = cap.capability_score()
    assert sc.kind == "capability"
    assert 0 < sc.value <= 1
    assert set(sc.components) == {"success", "quality", "consistency"}
