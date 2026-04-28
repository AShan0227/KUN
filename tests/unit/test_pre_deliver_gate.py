"""V2.3+ PreDeliverGate — 任务交付前产品级审核."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kun.engineering.pre_deliver_gate import (
    GateCheckResult,
    PreDeliverGate,
    PreDeliverVerdict,
)


def _fake_task_ref(verification_specs=None):
    spec = None
    if verification_specs is not None:
        spec = SimpleNamespace(verification_specs=verification_specs)
    return SimpleNamespace(
        meta=SimpleNamespace(task_id="tk-test", task_type="writing.test"),
        spec=spec,
    )


def _fake_plan(steps=2):
    return SimpleNamespace(steps=[SimpleNamespace() for _ in range(steps)])


def test_gate_is_enabled_default_on() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_PRE_DELIVER_GATE_ENABLED", None)
        assert PreDeliverGate.is_enabled() is True


def test_gate_is_enabled_off() -> None:
    with patch.dict(os.environ, {"KUN_PRE_DELIVER_GATE_ENABLED": "0"}):
        assert PreDeliverGate.is_enabled() is False


@pytest.mark.asyncio
async def test_review_empty_answer_critical_fail() -> None:
    gate = PreDeliverGate()
    verdict = await gate.review(
        answer="",
        task_ref=_fake_task_ref(),
        plan=_fake_plan(),
        step_records=[],
    )
    assert verdict.passed is False
    assert verdict.final_status == "failed"
    assert verdict.has_critical
    assert any(c.name == "self_check.empty_output" for c in verdict.checks)


@pytest.mark.asyncio
async def test_review_short_output_critical() -> None:
    gate = PreDeliverGate()
    verdict = await gate.review(
        answer="hi",
        task_ref=_fake_task_ref(),
        plan=_fake_plan(),
        step_records=[],
    )
    assert verdict.final_status == "failed"


@pytest.mark.asyncio
async def test_review_normal_output_passes() -> None:
    gate = PreDeliverGate()
    verdict = await gate.review(
        answer="这是一段正常的产品输出, 至少 5 字符",
        task_ref=_fake_task_ref(),
        plan=_fake_plan(),
        step_records=[],
    )
    assert verdict.passed is True
    assert verdict.final_status == "done"


@pytest.mark.asyncio
async def test_review_error_prefix_high_severity() -> None:
    gate = PreDeliverGate()
    verdict = await gate.review(
        answer="Error: something broke at line 42",
        task_ref=_fake_task_ref(),
        plan=_fake_plan(),
        step_records=[],
    )
    # high severity → needs_review
    assert verdict.passed is False
    assert verdict.final_status == "needs_review"
    assert any(c.name == "self_check.error_output" for c in verdict.checks)


@pytest.mark.asyncio
async def test_review_with_anti_gaming_detector_clean() -> None:
    from kun.security.anti_gaming import AntiGamingDetector

    det = AntiGamingDetector(off_topic_threshold=0.05)
    gate = PreDeliverGate(anti_gaming_detector=det)
    verdict = await gate.review(
        answer="A clean and substantial answer with content",
        task_ref=_fake_task_ref(),
        plan=_fake_plan(steps=2),
        step_records=[
            SimpleNamespace(skill_used="reader"),
            SimpleNamespace(skill_used="writer"),
        ],
    )
    # Anti-gaming overall scan should pass for clean answer
    assert any(c.name == "anti_gaming.overall" and c.passed for c in verdict.checks)


@pytest.mark.asyncio
async def test_review_with_failing_required_verification() -> None:
    """required verification fail → high severity → needs_review."""
    from kun.datamodel.verification_spec import VerificationResult, VerificationSpec

    class _FailRunner:
        async def verify(self, spec, artifact):
            return VerificationResult(kind=spec.kind, passed=False, error_msg="fail")

    spec = VerificationSpec(kind="exact_output", spec={}, required=True)
    gate = PreDeliverGate(verification_runner=_FailRunner())
    verdict = await gate.review(
        answer="A reasonably long answer to avoid self-check fail",
        task_ref=_fake_task_ref(verification_specs=[spec]),
        plan=_fake_plan(),
        step_records=[],
    )
    # required fail → high → needs_review
    assert verdict.final_status == "needs_review"


@pytest.mark.asyncio
async def test_review_with_passing_verification() -> None:
    from kun.datamodel.verification_spec import VerificationResult, VerificationSpec

    class _PassRunner:
        async def verify(self, spec, artifact):
            return VerificationResult(kind=spec.kind, passed=True, error_msg="")

    spec = VerificationSpec(kind="exact_output", spec={}, required=True)
    gate = PreDeliverGate(verification_runner=_PassRunner())
    verdict = await gate.review(
        answer="A reasonably long answer to avoid self-check fail",
        task_ref=_fake_task_ref(verification_specs=[spec]),
        plan=_fake_plan(),
        step_records=[],
    )
    assert verdict.passed is True
    assert verdict.final_status == "done"


@pytest.mark.asyncio
async def test_review_optional_verification_failure_doesnt_block() -> None:
    from kun.datamodel.verification_spec import VerificationResult, VerificationSpec

    class _FailRunner:
        async def verify(self, spec, artifact):
            return VerificationResult(kind=spec.kind, passed=False, error_msg="fail")

    spec = VerificationSpec(kind="lint_pass", spec={}, required=False)  # optional
    gate = PreDeliverGate(verification_runner=_FailRunner())
    verdict = await gate.review(
        answer="A reasonably long answer to avoid self-check fail",
        task_ref=_fake_task_ref(verification_specs=[spec]),
        plan=_fake_plan(),
        step_records=[],
    )
    # optional fail → low severity → passes overall
    assert verdict.passed is True
    assert verdict.final_status == "done"


def test_gate_check_result_dataclass() -> None:
    c = GateCheckResult(name="x", passed=True)
    assert c.severity == "low"
    assert c.evidence == {}


def test_pre_deliver_verdict_has_critical() -> None:
    v = PreDeliverVerdict(
        passed=False,
        checks=[
            GateCheckResult(name="x", passed=False, severity="critical"),
        ],
    )
    assert v.has_critical is True
    assert v.has_high is False
