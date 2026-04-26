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


# V2.2 §21 wire: ExecutionMode override


def _mk_with_mode(
    risk: str, complexity: float, mode: str, reason: str = "classifier_set"
) -> TaskMeta:
    owner = Owner(tenant_id="u-sylvan")
    return TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type="general.default",
        risk_level=risk,  # type: ignore[arg-type]
        complexity_score=complexity,
        owner=owner,
        success_criteria_short="t",
        execution_mode=mode,  # type: ignore[arg-type]
        mode_override_reason=reason,
    )


@pytest.mark.unit
def test_mode_fast_forces_tier0_even_with_high_risk():
    """V2.2 §21: FAST 强制 tier0, 即使 risk=critical."""
    assert pick_tier(_mk_with_mode("critical", 0.9, "FAST")) == "tier0"


@pytest.mark.unit
def test_mode_max_forces_tier3_even_with_low_risk():
    """V2.2 §21: MAX 强制 tier3, 即使 risk=low + low complexity."""
    assert pick_tier(_mk_with_mode("low", 0.1, "MAX")) == "tier3"


@pytest.mark.unit
def test_mode_smart_uses_old_matrix():
    """V2.2 §21: SMART 不强制, 仍走 risk × complexity 矩阵."""
    assert pick_tier(_mk_with_mode("high", 0.8, "SMART")) == "tier3"
    assert pick_tier(_mk_with_mode("low", 0.2, "SMART")) == "tier0"


@pytest.mark.unit
def test_mode_default_no_reason_uses_old_matrix():
    """没显式 mode_override_reason → 不被 mode 覆盖, 走老矩阵 (向后兼容)."""
    assert pick_tier(_mk_with_mode("critical", 0.9, "FAST", reason="")) == "tier3"
