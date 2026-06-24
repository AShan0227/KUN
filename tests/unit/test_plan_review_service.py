"""LT.B — PlanReviewService 单测 (ADR-022 Layer 4 runtime wiring)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from kun.agents.supervisor.plan_review_heartbeat import PlanReviewHeartbeat
from kun.agents.supervisor.plan_review_prompt import render_plan_review_prompt
from kun.agents.supervisor.plan_review_service import (
    PlanReviewOutcome,
    PlanReviewService,
)

# ---- Stubs ----


@dataclass
class _FakeExternalObservation:
    """Minimal stub mimicking ExternalSupervisorObservation."""

    verdict: str  # ok / concerning / alarming
    rationale: str = ""


def _aligned_self_report() -> dict[str, Any]:
    return {
        "current_step": "writing tests",
        "on_anchor": True,
        "scope_creep_detected": False,
        "criteria_done_count": 2,
        "criteria_done_count_prev": 2,
        "recent_step_summary": "added oauth login token refresh tests",
        "drift_risk": "low",
    }


def _drifting_self_report() -> dict[str, Any]:
    return {
        "current_step": "writing tests",
        "on_anchor": True,
        "scope_creep_detected": True,  # 1 drift signal
        "criteria_done_count": 2,
        "criteria_done_count_prev": 2,
        "recent_step_summary": "oauth tests",
        "drift_risk": "medium",
    }


def _off_track_self_report() -> dict[str, Any]:
    return {
        "current_step": "browsing the weather",
        "on_anchor": False,  # 1 drift signal
        "scope_creep_detected": True,  # 2nd signal
        "criteria_done_count": 1,
        "criteria_done_count_prev": 2,  # regression — 3rd signal
        "recent_step_summary": "today's weather looks nice",
        "drift_risk": "high",
    }


# ---- render_plan_review_prompt ----


def test_render_prompt_contains_required_sections() -> None:
    prompt = render_plan_review_prompt(reason="step_threshold")
    assert "PLAN REVIEW" in prompt
    assert "step_threshold" in prompt
    assert "on_anchor" in prompt
    assert "scope_creep_detected" in prompt
    assert "drift_risk" in prompt


def test_render_prompt_includes_recent_steps() -> None:
    prompt = render_plan_review_prompt(
        reason="time_threshold",
        recent_steps=[
            "ran pytest, 14 passed",
            "edited login.py",
            "edited tests/login.py",
        ],
    )
    assert "edited login.py" in prompt
    assert "ran pytest" in prompt


def test_render_prompt_truncates_long_step_to_100() -> None:
    long_step = "x" * 200
    prompt = render_plan_review_prompt(
        reason="step_threshold", recent_steps=[long_step]
    )
    assert "..." in prompt


def test_render_prompt_only_last_3_recent_steps() -> None:
    steps = [f"step-{i}" for i in range(10)]
    prompt = render_plan_review_prompt(
        reason="step_threshold", recent_steps=steps
    )
    # 应该只有最后 3 步
    assert "step-7" in prompt
    assert "step-8" in prompt
    assert "step-9" in prompt
    assert "step-0" not in prompt


def test_render_prompt_includes_extras() -> None:
    prompt = render_plan_review_prompt(
        reason="step_threshold",
        extras={"total_steps": 12, "total_reviews": 4},
    )
    assert "total_steps: 12" in prompt
    assert "total_reviews: 4" in prompt


# ---- PlanReviewService.observe_step_and_maybe_render_prompt ----


@pytest.mark.asyncio
async def test_observe_step_returns_none_before_threshold() -> None:
    heartbeat = PlanReviewHeartbeat(step_interval=3, time_interval_sec=3600)
    service = PlanReviewService(heartbeat=heartbeat)
    # First step — not yet at threshold
    prompt = await service.observe_step_and_maybe_render_prompt(
        task_id="tk-1", anchor_id="ga-1"
    )
    assert prompt is None


@pytest.mark.asyncio
async def test_observe_step_renders_prompt_at_step_threshold() -> None:
    heartbeat = PlanReviewHeartbeat(step_interval=2, time_interval_sec=3600)
    service = PlanReviewService(heartbeat=heartbeat)
    # Step 1 — no prompt
    await service.observe_step_and_maybe_render_prompt(
        task_id="tk-1", anchor_id="ga-1"
    )
    # Step 2 — at threshold
    prompt = await service.observe_step_and_maybe_render_prompt(
        task_id="tk-1", anchor_id="ga-1"
    )
    assert prompt is not None
    assert "PLAN REVIEW" in prompt
    assert "step_threshold" in prompt


@pytest.mark.asyncio
async def test_observe_step_prompt_carries_recent_steps_and_extras() -> None:
    heartbeat = PlanReviewHeartbeat(step_interval=1, time_interval_sec=3600)
    service = PlanReviewService(heartbeat=heartbeat)
    prompt = await service.observe_step_and_maybe_render_prompt(
        task_id="tk-1",
        anchor_id="ga-1",
        recent_steps=["edited file A", "ran tests"],
    )
    assert prompt is not None
    assert "edited file A" in prompt
    assert "total_steps" in prompt


# ---- PlanReviewService.submit_self_report — internal only ----


@pytest.mark.asyncio
async def test_submit_aligned_self_report_returns_continue() -> None:
    heartbeat = PlanReviewHeartbeat()
    service = PlanReviewService(heartbeat=heartbeat)
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_aligned_self_report(),
    )
    assert isinstance(outcome, PlanReviewOutcome)
    assert outcome.internal_verdict == "aligned"
    assert outcome.external_verdict is None
    assert outcome.final_verdict == "aligned"
    assert outcome.action == "continue"


@pytest.mark.asyncio
async def test_submit_drifting_self_report_pauses_for_recheck() -> None:
    heartbeat = PlanReviewHeartbeat()
    service = PlanReviewService(heartbeat=heartbeat)
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_drifting_self_report(),
    )
    assert outcome.internal_verdict == "drifting"
    assert outcome.action == "pause_for_anchor_recheck"


@pytest.mark.asyncio
async def test_submit_off_track_self_report_triggers_rcdh() -> None:
    heartbeat = PlanReviewHeartbeat()
    service = PlanReviewService(heartbeat=heartbeat)
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_off_track_self_report(),
    )
    assert outcome.internal_verdict == "off_track"
    assert outcome.action == "trigger_rcdh_level_0"
    # 3 drift signals collected
    assert len(outcome.drift_evidence) >= 2


# ---- External Supervisor verify path ----


@pytest.mark.asyncio
async def test_external_concerning_promotes_aligned_to_drifting() -> None:
    """internal=aligned, external=concerning → final=drifting (取严)."""
    heartbeat = PlanReviewHeartbeat()
    captured: list[tuple] = []

    async def fake_verify(self_report, anchor):
        captured.append((self_report, anchor))
        return _FakeExternalObservation(
            verdict="concerning", rationale="recent step doesn't seem to advance criteria"
        )

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=fake_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_aligned_self_report(),
        anchor_dict={"goal_statement": "implement oauth"},
    )
    assert outcome.internal_verdict == "aligned"
    assert outcome.external_verdict == "drifting"
    assert outcome.final_verdict == "drifting"
    assert outcome.action == "pause_for_anchor_recheck"
    # external verify was called with self_report and anchor_dict
    assert len(captured) == 1
    assert captured[0][1]["goal_statement"] == "implement oauth"


@pytest.mark.asyncio
async def test_external_alarming_promotes_aligned_to_off_track() -> None:
    """internal=aligned, external=alarming → final=off_track."""
    heartbeat = PlanReviewHeartbeat()

    async def fake_verify(self_report, anchor):
        return _FakeExternalObservation(verdict="alarming", rationale="fabricated evidence")

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=fake_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_aligned_self_report(),
    )
    assert outcome.internal_verdict == "aligned"
    assert outcome.external_verdict == "off_track"
    assert outcome.final_verdict == "off_track"
    assert outcome.action == "trigger_rcdh_level_0"


@pytest.mark.asyncio
async def test_external_ok_does_not_demote_internal_off_track() -> None:
    """internal=off_track, external=ok → final=off_track (取严, 不被外部 OK 降低)."""
    heartbeat = PlanReviewHeartbeat()

    async def fake_verify(self_report, anchor):
        return _FakeExternalObservation(verdict="ok", rationale="looks fine")

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=fake_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_off_track_self_report(),
    )
    assert outcome.internal_verdict == "off_track"
    assert outcome.external_verdict == "aligned"
    assert outcome.final_verdict == "off_track"  # 取严
    assert outcome.action == "trigger_rcdh_level_0"


@pytest.mark.asyncio
async def test_external_verify_failure_does_not_break_main_path() -> None:
    """External Supervisor 调用 raise → internal verdict 仍生效, external=None."""
    heartbeat = PlanReviewHeartbeat()

    async def bad_verify(self_report, anchor):
        raise RuntimeError("ollama down")

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=bad_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_drifting_self_report(),
    )
    assert outcome.internal_verdict == "drifting"
    assert outcome.external_verdict is None
    assert outcome.final_verdict == "drifting"
    assert outcome.action == "pause_for_anchor_recheck"


@pytest.mark.asyncio
async def test_external_unrecognized_verdict_treated_as_none() -> None:
    """External Supervisor 返回非 ok/concerning/alarming → 忽略 (None), 仅用 internal."""
    heartbeat = PlanReviewHeartbeat()

    async def fake_verify(self_report, anchor):
        return _FakeExternalObservation(verdict="garbage_value", rationale="x")

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=fake_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_aligned_self_report(),
    )
    assert outcome.internal_verdict == "aligned"
    assert outcome.external_verdict is None
    assert outcome.final_verdict == "aligned"


# ---- rationale field ----


@pytest.mark.asyncio
async def test_outcome_rationale_carries_both_verdicts() -> None:
    heartbeat = PlanReviewHeartbeat()

    async def fake_verify(self_report, anchor):
        return _FakeExternalObservation(
            verdict="concerning", rationale="step looks tangential"
        )

    service = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=fake_verify,
    )
    outcome = await service.submit_self_report(
        task_id="tk-1",
        anchor_id="ga-1",
        executor_self_report=_aligned_self_report(),
    )
    assert "internal=aligned" in outcome.rationale
    assert "external=drifting" in outcome.rationale
    assert "step looks tangential" in outcome.rationale
    assert "action=pause_for_anchor_recheck" in outcome.rationale
