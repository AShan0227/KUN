"""V7 Phase B — Mission Director 一级子系统 单测.

V7 §9.7 task-level supervision service.
"""

from __future__ import annotations

import pytest
from kun.agents.mission_director import (
    AlignmentVerdict,
    MissionDirector,
    MissionDirectorService,
    PlanChangeSeverity,
)


@pytest.mark.unit
def test_mission_director_alias() -> None:
    """V7 §9.7 公共 alias."""
    assert MissionDirector is MissionDirectorService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_perfect_alignment() -> None:
    """所有 coverage = 1.0 → verdict OK."""
    svc = MissionDirectorService()
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=1.0,
        decomposition_coverage=1.0,
        evidence_coverage=1.0,
    )
    assert review.verdict == AlignmentVerdict.OK
    assert review.alignment_score == pytest.approx(1.0)
    assert review.task_id == "tk-001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_drifting() -> None:
    """中等 coverage → drifting."""
    svc = MissionDirectorService()
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=0.6,
        decomposition_coverage=0.6,
        evidence_coverage=0.6,
    )
    # 0.4*0.6 + 0.35*0.6 + 0.25*0.6 = 0.6
    assert review.alignment_score == pytest.approx(0.6)
    assert review.verdict == AlignmentVerdict.DRIFTING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_off_anchor() -> None:
    """低 coverage → off_anchor."""
    svc = MissionDirectorService()
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=0.3,
        decomposition_coverage=0.3,
        evidence_coverage=0.3,
    )
    # 0.4*0.3 + 0.35*0.3 + 0.25*0.3 = 0.3
    assert review.alignment_score == pytest.approx(0.3)
    assert review.verdict == AlignmentVerdict.OFF_ANCHOR


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_needs_human() -> None:
    """极低 coverage → needs_human (强信号, 必须人审)."""
    svc = MissionDirectorService()
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=0.1,
        decomposition_coverage=0.1,
        evidence_coverage=0.1,
    )
    # 0.4*0.1 + 0.35*0.1 + 0.25*0.1 = 0.1
    assert review.alignment_score == pytest.approx(0.1)
    assert review.verdict == AlignmentVerdict.NEEDS_HUMAN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_auto_generated_findings_for_low_coverage() -> None:
    svc = MissionDirectorService()
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=0.3,  # < 0.5 → 自动加 finding
        decomposition_coverage=0.5,  # < 0.7 → 自动加 finding
        evidence_coverage=0.3,  # < 0.5 → 自动加 finding
    )
    assert len(review.findings) >= 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_emitter_called() -> None:
    captured = []

    async def _emitter(review):
        captured.append(review)

    svc = MissionDirectorService(review_emitter=_emitter)
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=1.0,
        decomposition_coverage=1.0,
        evidence_coverage=1.0,
    )
    assert len(captured) == 1
    assert captured[0].review_id == review.review_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_emitter_failure_does_not_crash() -> None:
    async def _bad_emitter(review):
        raise RuntimeError("DB down")

    svc = MissionDirectorService(review_emitter=_bad_emitter)
    review = await svc.review_mission(
        task_id="tk-001",
        task_plan_version="tpv-001",
        info_gap_coverage=1.0,
        decomposition_coverage=1.0,
        evidence_coverage=1.0,
    )
    assert review.review_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_propose_plan_change_low_severity() -> None:
    svc = MissionDirectorService()
    proposal = await svc.propose_plan_change(
        task_id="tk-001",
        change_type="scope",
        severity=PlanChangeSeverity.LOW,
        affected_work_items=["wi-1"],
        affected_deliverables=[],
        candidate_changes=[{"diff": "reorder subtasks"}],
        rollback_condition="any test failure",
        rationale="子任务顺序优化",
    )
    assert proposal.severity == PlanChangeSeverity.LOW
    assert proposal.user_approval_required is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_propose_plan_change_high_severity_requires_user_approval() -> None:
    """V7 §10.3.3 high severity → user_approval_required=True."""
    svc = MissionDirectorService()
    proposal = await svc.propose_plan_change(
        task_id="tk-001",
        change_type="scope",
        severity=PlanChangeSeverity.HIGH,
        affected_work_items=["wi-1"],
        affected_deliverables=["deliverable-1"],
        candidate_changes=[{"diff": "change mission boundary"}],
        rollback_condition="user reject",
        rationale="任务边界变更",
    )
    assert proposal.severity == PlanChangeSeverity.HIGH
    assert proposal.user_approval_required is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_propose_plan_change_medium_severity_no_user_approval() -> None:
    """V7 §10.3.3 medium severity → KUN 自动改 (但 caller 应推 NUO panel)."""
    svc = MissionDirectorService()
    proposal = await svc.propose_plan_change(
        task_id="tk-001",
        change_type="criteria",
        severity=PlanChangeSeverity.MEDIUM,
        affected_work_items=["wi-1"],
        affected_deliverables=[],
        candidate_changes=[{"diff": "add holdout step"}],
        rollback_condition="indicator regression",
        rationale="加 holdout 验证步骤",
    )
    assert proposal.severity == PlanChangeSeverity.MEDIUM
    assert proposal.user_approval_required is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_propose_plan_change_emitter_called() -> None:
    captured = []

    async def _emitter(p):
        captured.append(p)

    svc = MissionDirectorService(proposal_emitter=_emitter)
    proposal = await svc.propose_plan_change(
        task_id="tk-001",
        change_type="scope",
        severity=PlanChangeSeverity.LOW,
        affected_work_items=[],
        affected_deliverables=[],
        candidate_changes=[],
        rollback_condition="",
        rationale="",
    )
    assert len(captured) == 1
    assert captured[0].proposal_id == proposal.proposal_id


@pytest.mark.unit
def test_review_to_row_payload() -> None:
    """V7 §13.6 IO 契约: to_row_payload() helper."""
    from datetime import UTC, datetime

    from kun.agents.mission_director import MissionAlignmentReview

    review = MissionAlignmentReview(
        review_id="mar-001",
        task_id="tk-001",
        task_plan_version="tpv-001",
        reviewed_at=datetime.now(UTC),
        verdict=AlignmentVerdict.OK,
        alignment_score=0.9,
        findings=[],
        info_gap_coverage=0.9,
        decomposition_coverage=0.9,
        evidence_coverage=0.9,
    )
    payload = review.to_row_payload(tenant_id="t-1")
    assert payload["tenant_id"] == "t-1"
    assert payload["review_id"] == "mar-001"
    assert payload["verdict"] == "ok"


@pytest.mark.unit
def test_proposal_to_row_payload() -> None:
    from datetime import UTC, datetime

    from kun.agents.mission_director import PlanChangeProposal

    proposal = PlanChangeProposal(
        proposal_id="pcp-001",
        task_id="tk-001",
        triggered_by="mission_director",
        triggered_at=datetime.now(UTC),
        change_type="scope",
        severity=PlanChangeSeverity.HIGH,
        affected_work_items=["wi-1"],
        affected_deliverables=["d-1"],
        candidate_changes=[],
        rollback_condition="x",
        rationale="y",
        user_approval_required=True,
    )
    payload = proposal.to_row_payload(tenant_id="t-1")
    assert payload["severity"] == "high"
    assert payload["user_approval_required"] is True
