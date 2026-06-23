"""Validation tier selection matrix (§8.1)."""

import pytest
from kun.agents.tester.validation import (
    ValidationPipeline,
    ValidationResult,
    pick_tier,
)
from kun.core.scoring import ScoreDescriptor
from kun.datamodel.task import Owner, TaskMeta


def _res(kind: str, ok: bool, val: float) -> ValidationResult:
    return ValidationResult(
        validator_kind=kind,  # type: ignore[arg-type]
        pass_=ok,
        score=ScoreDescriptor(kind="rubric", value=val),
    )


@pytest.mark.unit
def test_aggregate_mixed_kinds_yields_ensemble_without_crash() -> None:
    # F020 regression: multiple distinct validator kinds → "ensemble", which must
    # be a valid ValidatorKind member or ValidationResult fails Pydantic and the
    # tier-3 aggregation crashes.
    out = ValidationPipeline.aggregate([_res("multi_judge", True, 0.9), _res("debate", True, 0.8)])
    assert out is not None
    assert out.validator_kind == "ensemble"
    assert out.pass_ is True
    assert out.score.value == pytest.approx(0.85)


@pytest.mark.unit
def test_aggregate_single_kind_preserves_kind_and_all_pass_policy() -> None:
    out = ValidationPipeline.aggregate(
        [_res("single_judge", True, 0.9), _res("single_judge", False, 0.4)]
    )
    assert out is not None
    assert out.validator_kind == "single_judge"
    assert out.pass_ is False  # all_pass policy


@pytest.mark.unit
def test_aggregate_empty_returns_none() -> None:
    assert ValidationPipeline.aggregate([]) is None


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
