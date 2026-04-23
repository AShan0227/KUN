"""Experiments: consistent hash bucketing."""

import pytest
from kun.engineering.experiments import pick_variant


@pytest.mark.unit
def test_pick_variant_stable_for_same_subject():
    # Same subject + experiment always yields same variant
    for _ in range(10):
        assert pick_variant("exp1", "user-a", 50) == pick_variant("exp1", "user-a", 50)


@pytest.mark.unit
def test_pick_variant_0_percent_is_control():
    assert pick_variant("exp1", "any-subject", 0) == "control"


@pytest.mark.unit
def test_pick_variant_100_percent_is_treatment():
    assert pick_variant("exp1", "any-subject", 100) == "treatment"


@pytest.mark.unit
def test_pick_variant_distribution_approx():
    # Over many subjects, rollout_percent should roughly match treatment ratio
    n = 5000
    treatment = sum(1 for i in range(n) if pick_variant("exp1", f"user-{i}", 30) == "treatment")
    ratio = treatment / n
    assert 0.27 < ratio < 0.33


@pytest.mark.unit
def test_pick_variant_sticky_as_rollout_grows():
    # Subjects in treatment at 30% should all still be in treatment at 50%.
    subjects_in_treatment_at_30 = [
        f"user-{i}" for i in range(500) if pick_variant("exp1", f"user-{i}", 30) == "treatment"
    ]
    for s in subjects_in_treatment_at_30:
        assert pick_variant("exp1", s, 50) == "treatment"
