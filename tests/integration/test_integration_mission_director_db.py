"""Integration: MissionDirectorService → DB writer (V7 §9.7 Phase X.B).

Validates the to_row_payload → Row construction + session_scope bind-and-flush
path for kun.integration.mission_director_db. Uses an in-process fake
session_scope (same pattern as test_integration_plan_review_db.py) so no
Postgres needed — but exercises the real MissionAlignmentReviewRow /
PlanChangeProposalRow construction.

Coverage:
  - write_mission_review with each verdict (ok/drifting/off_anchor/needs_human)
  - write_plan_change_proposal with low/medium/high severity
  - high severity → user_approval_required=True invariant honored
  - make_*_emitter factories — emitter callable signature + tenant_id binding
  - MissionDirectorService 真用 emitter (service.review_mission triggers write)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from kun.agents.mission_director.service import (
    AlignmentVerdict,
    MissionAlignmentReview,
    MissionDirectorService,
    PlanChangeProposal,
    PlanChangeSeverity,
)
from kun.core.orm import MissionAlignmentReviewRow, PlanChangeProposalRow
from kun.integration.mission_director_db import (
    make_mission_review_emitter,
    make_plan_change_proposal_emitter,
    write_mission_review,
    write_plan_change_proposal,
)

# ============================================================
# Fake session helper (same pattern as plan_review_db tests)
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:  # pragma: no cover - trivial
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[dict[str, Any]]]:
    added: list[Any] = []
    scope_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        scope_calls.append(kwargs)
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    return added, scope_calls


def _make_review(
    *,
    review_id: str = "mar-test-1",
    task_id: str = "task-1",
    verdict: AlignmentVerdict = AlignmentVerdict.OK,
    alignment_score: float = 0.85,
    info_gap_coverage: float = 0.9,
    decomposition_coverage: float = 0.85,
    evidence_coverage: float = 0.7,
    findings: list[str] | None = None,
    plan_change_proposed: bool = False,
    plan_change_proposal_id: str | None = None,
) -> MissionAlignmentReview:
    return MissionAlignmentReview(
        review_id=review_id,
        task_id=task_id,
        task_plan_version="v1",
        reviewed_at=datetime.now(UTC),
        verdict=verdict,
        alignment_score=alignment_score,
        findings=list(findings or []),
        info_gap_coverage=info_gap_coverage,
        decomposition_coverage=decomposition_coverage,
        evidence_coverage=evidence_coverage,
        plan_change_proposed=plan_change_proposed,
        plan_change_proposal_id=plan_change_proposal_id,
    )


def _make_proposal(
    *,
    proposal_id: str = "pcp-test-1",
    task_id: str = "task-1",
    severity: PlanChangeSeverity = PlanChangeSeverity.LOW,
    change_type: str = "scope",
    triggered_by: str = "mission_director",
    candidate_changes: list[dict[str, Any]] | None = None,
    user_approval_required: bool | None = None,
) -> PlanChangeProposal:
    if user_approval_required is None:
        user_approval_required = severity == PlanChangeSeverity.HIGH
    return PlanChangeProposal(
        proposal_id=proposal_id,
        task_id=task_id,
        triggered_by=triggered_by,
        triggered_at=datetime.now(UTC),
        change_type=change_type,
        severity=severity,
        affected_work_items=["wi-1"],
        affected_deliverables=["dlv-a"],
        candidate_changes=list(candidate_changes or [{"name": "default", "delta": "+1"}]),
        rollback_condition="alignment_score < 0.3",
        rationale="test rationale",
        user_approval_required=user_approval_required,
    )


# ============================================================
# write_mission_review — all 4 verdicts
# ============================================================


@pytest.mark.parametrize(
    "verdict,score",
    [
        (AlignmentVerdict.OK, 0.85),
        (AlignmentVerdict.DRIFTING, 0.6),
        (AlignmentVerdict.OFF_ANCHOR, 0.3),
        (AlignmentVerdict.NEEDS_HUMAN, 0.1),
    ],
)
async def test_write_mission_review_all_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    verdict: AlignmentVerdict,
    score: float,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    review = _make_review(verdict=verdict, alignment_score=score)
    returned_id = await write_mission_review(tenant_id="tenant-a", review=review)

    assert returned_id == review.review_id
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, MissionAlignmentReviewRow)
    assert row.tenant_id == "tenant-a"
    assert row.review_id == review.review_id
    assert row.task_id == "task-1"
    assert row.verdict == verdict.value
    assert float(row.alignment_score) == pytest.approx(score)
    # session_scope received tenant_id for RLS GUC
    assert scope_calls == [{"tenant_id": "tenant-a"}]


async def test_write_mission_review_persists_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    findings = ["info_gap A 未补", "decomposition 漏 1 个 deliverable"]
    review = _make_review(findings=findings)

    await write_mission_review(tenant_id="tenant-b", review=review)

    row = added[0]
    assert row.findings == findings


async def test_write_mission_review_with_proposal_linkage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    review = _make_review(
        plan_change_proposed=True,
        plan_change_proposal_id="pcp-linked-1",
    )

    await write_mission_review(tenant_id="tenant-c", review=review)

    row = added[0]
    assert row.plan_change_proposed is True
    assert row.plan_change_proposal_id == "pcp-linked-1"


# ============================================================
# write_plan_change_proposal — severity matrix
# ============================================================


@pytest.mark.parametrize(
    "severity,expected_user_approval",
    [
        (PlanChangeSeverity.LOW, False),
        (PlanChangeSeverity.MEDIUM, False),
        (PlanChangeSeverity.HIGH, True),
    ],
)
async def test_write_plan_change_proposal_severity_matrix(
    monkeypatch: pytest.MonkeyPatch,
    severity: PlanChangeSeverity,
    expected_user_approval: bool,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    proposal = _make_proposal(severity=severity)
    returned_id = await write_plan_change_proposal(
        tenant_id="tenant-a", proposal=proposal
    )

    assert returned_id == proposal.proposal_id
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, PlanChangeProposalRow)
    assert row.severity == severity.value
    assert row.user_approval_required is expected_user_approval
    assert scope_calls == [{"tenant_id": "tenant-a"}]


async def test_proposal_candidate_changes_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    candidates = [
        {"name": "option-a", "delta": "scope -1"},
        {"name": "option-b", "delta": "scope -2"},
    ]
    proposal = _make_proposal(candidate_changes=candidates)

    await write_plan_change_proposal(tenant_id="tenant-x", proposal=proposal)

    row = added[0]
    assert row.candidate_changes == candidates
    assert row.affected_work_items == ["wi-1"]
    assert row.affected_deliverables == ["dlv-a"]


async def test_proposal_change_type_and_triggered_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    proposal = _make_proposal(change_type="risk", triggered_by="qi")

    await write_plan_change_proposal(tenant_id="tenant-y", proposal=proposal)

    row = added[0]
    assert row.change_type == "risk"
    assert row.triggered_by == "qi"


# ============================================================
# Factories — emitter callable + tenant_id binding
# ============================================================


async def test_make_mission_review_emitter_binds_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    emitter = make_mission_review_emitter("tenant-emitter")
    review = _make_review()
    await emitter(review)

    assert len(added) == 1
    assert scope_calls == [{"tenant_id": "tenant-emitter"}]


async def test_make_plan_change_proposal_emitter_binds_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    emitter = make_plan_change_proposal_emitter("tenant-emitter-2")
    proposal = _make_proposal()
    await emitter(proposal)

    assert len(added) == 1
    assert scope_calls == [{"tenant_id": "tenant-emitter-2"}]


# ============================================================
# E2E — MissionDirectorService actually uses the emitter
# ============================================================


async def test_service_review_mission_writes_to_db_via_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: instantiate service with real DB emitter, run review,
    confirm a row was added to the in-memory session capture."""
    added, scope_calls = _install_fake_session(monkeypatch)

    service = MissionDirectorService(
        review_emitter=make_mission_review_emitter("tenant-e2e"),
    )

    review = await service.review_mission(
        task_id="task-e2e-1",
        task_plan_version="v3",
        info_gap_coverage=0.9,
        decomposition_coverage=0.8,
        evidence_coverage=0.7,
    )

    # Service computed a real verdict
    assert review.verdict == AlignmentVerdict.OK
    # And the emitter actually wrote to "DB"
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, MissionAlignmentReviewRow)
    assert row.tenant_id == "tenant-e2e"
    assert row.task_id == "task-e2e-1"
    assert row.task_plan_version == "v3"
    assert scope_calls == [{"tenant_id": "tenant-e2e"}]


async def test_service_propose_plan_change_writes_to_db_via_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    service = MissionDirectorService(
        proposal_emitter=make_plan_change_proposal_emitter("tenant-prop"),
    )

    proposal = await service.propose_plan_change(
        task_id="task-prop-1",
        change_type="scope",
        severity=PlanChangeSeverity.HIGH,
        affected_work_items=["wi-1", "wi-2"],
        affected_deliverables=["dlv-1"],
        candidate_changes=[{"name": "cut", "delta": "-2 wi"}],
        rollback_condition="evidence_coverage < 0.2",
        rationale="info_gap 2/3 未补",
    )

    # Service computed user_approval_required for HIGH severity
    assert proposal.user_approval_required is True
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, PlanChangeProposalRow)
    assert row.severity == "high"
    assert row.user_approval_required is True
    assert row.change_type == "scope"


# ============================================================
# Schema sanity — Row exposes expected columns
# ============================================================


def test_row_classes_have_v7_schema_columns() -> None:
    """Catch silent column renames against V7 §9.7 schema."""
    mar_cols = {c.name for c in MissionAlignmentReviewRow.__table__.columns}
    expected_mar = {
        "tenant_id",
        "review_id",
        "task_id",
        "task_plan_version",
        "reviewed_at",
        "verdict",
        "alignment_score",
        "findings",
        "info_gap_coverage",
        "decomposition_coverage",
        "evidence_coverage",
        "plan_change_proposed",
        "plan_change_proposal_id",
        "created_at",
    }
    assert expected_mar.issubset(mar_cols), (
        f"MissionAlignmentReviewRow 缺列: {expected_mar - mar_cols}"
    )

    pcp_cols = {c.name for c in PlanChangeProposalRow.__table__.columns}
    expected_pcp = {
        "tenant_id",
        "proposal_id",
        "task_id",
        "triggered_by",
        "triggered_at",
        "change_type",
        "severity",
        "affected_work_items",
        "affected_deliverables",
        "candidate_changes",
        "rollback_condition",
        "rationale",
        "user_approval_required",
        "user_decision",
        "user_decided_at",
        "created_at",
    }
    assert expected_pcp.issubset(pcp_cols), (
        f"PlanChangeProposalRow 缺列: {expected_pcp - pcp_cols}"
    )
