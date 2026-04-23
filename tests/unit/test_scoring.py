"""ScoreDescriptor tests."""

from datetime import UTC, datetime, timedelta

import pytest
from kun.core.scoring import ScoreDescriptor, wilson_ci95
from pydantic import ValidationError


@pytest.mark.unit
def test_compose_weighted_sum():
    sc = ScoreDescriptor.compose(
        kind="importance",
        components={"a": 0.8, "b": 0.4, "c": 1.0},
        weights={"a": 0.5, "b": 0.3, "c": 0.2},
    )
    assert abs(sc.value - (0.8 * 0.5 + 0.4 * 0.3 + 1.0 * 0.2)) < 1e-9
    assert sc.kind == "importance"


@pytest.mark.unit
def test_compose_rejects_nonnormal_weights():
    with pytest.raises(ValidationError):
        ScoreDescriptor.compose(
            kind="capability",
            components={"x": 0.5, "y": 0.5},
            weights={"x": 0.7, "y": 0.7},  # sums 1.4
        )


@pytest.mark.unit
def test_decayed_value_no_half_life():
    sc = ScoreDescriptor(kind="importance", value=0.8)
    assert sc.decayed_value() == 0.8


@pytest.mark.unit
def test_decayed_value_exponential():
    sc = ScoreDescriptor(
        kind="importance",
        value=0.8,
        last_updated=datetime.now(UTC) - timedelta(days=11),
        decay_half_life_days=11,
    )
    # After exactly one half-life, value halves
    v = sc.decayed_value()
    assert 0.39 < v < 0.41


@pytest.mark.unit
def test_wilson_ci95():
    lo, hi = wilson_ci95(successes=90, trials=100)
    assert 0.82 < lo < 0.86
    assert 0.93 < hi < 0.96


@pytest.mark.unit
def test_wilson_ci95_empty():
    assert wilson_ci95(0, 0) is None


@pytest.mark.unit
def test_ci95_validation():
    with pytest.raises(ValidationError):
        ScoreDescriptor(kind="importance", value=0.5, ci95=(0.8, 0.2))
