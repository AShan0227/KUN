"""Validation tier selection matrix (§8.1)."""

import pytest
from kun.datamodel.task import Owner, TaskMeta
from kun.engineering.validation import pick_tier


def _mk(risk: str, complexity: float) -> TaskMeta:
    owner = Owner(tenant_id="u-sylvan")
    return TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type="general.default",
        risk_level=risk,  # type: ignore[arg-type]
        complexity_score=complexity,
        owner=owner,
        success_criteria_short="t",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "risk,complexity,expected",
    [
        ("low", 0.2, "tier0"),
        ("low", 0.8, "tier1"),
        ("medium", 0.2, "tier0"),
        ("high", 0.2, "tier2"),
        ("high", 0.8, "tier3"),
        ("critical", 0.1, "tier2"),
        ("critical", 0.9, "tier3"),
    ],
)
def test_tier_matrix(risk, complexity, expected):
    assert pick_tier(_mk(risk, complexity)) == expected
