"""Tests for FAST / SMART / MAX execution mode classification."""

import pytest
from kun.api.execution_mode_classifier import classify_execution_mode
from kun.datamodel.soul_file import SoulFile
from kun.datamodel.task import Owner, TaskMeta
from pydantic import ValidationError


def _soul(**kwargs):
    return SoulFile(user_id="u-1", tenant_id="t-1", **kwargs)


@pytest.mark.unit
def test_task_meta_defaults_to_fast_mode_fields():
    owner = Owner(tenant_id="t-1")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type="coding.python",
        owner=owner,
        success_criteria_short="done",
    )

    assert meta.execution_mode == "FAST"
    assert meta.mode_override_reason == ""


@pytest.mark.unit
def test_task_meta_rejects_invalid_execution_mode():
    owner = Owner(tenant_id="t-1")

    with pytest.raises(ValidationError):
        TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("x", owner),
            task_type="coding.python",
            owner=owner,
            success_criteria_short="done",
            execution_mode="TURBO",
        )


@pytest.mark.unit
def test_soul_file_default_execution_mode_preference():
    soul = _soul()

    assert soul.execution_mode_preference == {
        "default_mode": "FAST",
        "always_max_kinds": [],
        "always_fast_kinds": ["chitchat", "translate"],
    }


@pytest.mark.unit
def test_default_mode_fast():
    mode, reason = classify_execution_mode(
        {"task_type": "coding.python", "risk_level": "low", "complexity_score": 0.3},
        _soul(),
    )

    assert mode == "FAST"
    assert reason == "default_mode:FAST"


@pytest.mark.unit
def test_default_mode_can_come_from_soul_file_preference():
    mode, reason = classify_execution_mode(
        {"task_type": "research.summary", "risk_level": "low", "complexity_score": 0.1},
        _soul(execution_mode_preference={"default_mode": "SMART"}),
    )

    assert mode == "SMART"
    assert reason == "default_mode:SMART"


@pytest.mark.unit
def test_complexity_above_point_three_is_smart():
    mode, reason = classify_execution_mode(
        {"task_type": "coding.python", "risk_level": "low", "complexity_score": 0.31},
        _soul(),
    )

    assert mode == "SMART"
    assert reason == "complexity_score:0.31>0.3"


@pytest.mark.unit
def test_complexity_above_point_seven_is_max():
    mode, reason = classify_execution_mode(
        {"task_type": "coding.python", "risk_level": "low", "complexity_score": 0.71},
        _soul(),
    )

    assert mode == "MAX"
    assert reason == "complexity_score:0.71>0.7"


@pytest.mark.unit
def test_critical_risk_forces_max():
    mode, reason = classify_execution_mode(
        {"task_type": "translate.text", "risk_level": "critical", "complexity_score": 0.1},
        _soul(),
    )

    assert mode == "MAX"
    assert reason == "risk_level:critical"


@pytest.mark.unit
def test_cost_over_approval_threshold_forces_max():
    mode, reason = classify_execution_mode(
        {"task_type": "coding.python", "risk_level": "low", "estimated_cost_usd": 12.5},
        _soul(approval_threshold_money=10.0),
    )

    assert mode == "MAX"
    assert reason == "estimated_cost:12.5>approval_threshold_money:10"


@pytest.mark.unit
def test_cost_alias_estimated_cost_is_supported():
    mode, reason = classify_execution_mode(
        {"task_type": "coding.python", "risk_level": "low", "estimated_cost": "12.5"},
        _soul(approval_threshold_money=10.0),
    )

    assert mode == "MAX"
    assert reason == "estimated_cost:12.5>approval_threshold_money:10"


@pytest.mark.unit
def test_always_max_kind_forces_max():
    mode, reason = classify_execution_mode(
        {"task_kind": "deploy", "risk_level": "low", "complexity_score": 0.1},
        _soul(
            execution_mode_preference={
                "default_mode": "FAST",
                "always_max_kinds": ["deploy"],
                "always_fast_kinds": [],
            }
        ),
    )

    assert mode == "MAX"
    assert reason == "always_max_kind:deploy"


@pytest.mark.unit
def test_always_fast_kind_overrides_complexity():
    mode, reason = classify_execution_mode(
        {"task_type": "translate.document", "risk_level": "low", "complexity_score": 0.95},
        _soul(),
    )

    assert mode == "FAST"
    assert reason == "always_fast_kind:translate"


@pytest.mark.unit
def test_always_fast_kind_does_not_override_critical():
    mode, reason = classify_execution_mode(
        {"task_type": "chitchat.casual", "risk_level": "critical", "complexity_score": 0.1},
        _soul(),
    )

    assert mode == "MAX"
    assert reason == "risk_level:critical"


@pytest.mark.unit
def test_force_mode_overrides_high_cost():
    mode, reason = classify_execution_mode(
        {
            "task_type": "coding.python",
            "risk_level": "low",
            "estimated_cost_usd": 100.0,
            "force_mode": "smart",
        },
        _soul(approval_threshold_money=10.0),
    )

    assert mode == "SMART"
    assert reason == "force_mode:SMART"


@pytest.mark.unit
def test_invalid_force_mode_is_ignored():
    mode, reason = classify_execution_mode(
        {
            "task_type": "coding.python",
            "risk_level": "low",
            "complexity_score": 0.71,
            "force_mode": "TURBO",
        },
        _soul(),
    )

    assert mode == "MAX"
    assert reason == "complexity_score:0.71>0.7"
