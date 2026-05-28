"""Integration: CapabilityLifecycleService → DB writer (V7 §15 Phase X.B).

Validates LifecycleTransition → LifecycleTransitionRow path + tenant_id GUC
binding via fake session_scope (same pattern as test_integration_plan_review_db
+ test_integration_mission_director_db).

Coverage:
  - All 10 stage values round-trip through writer
  - CANDIDATE → REPLAY with 3 evidence kinds (V7 §12.3)
  - CANARY → PRODUCTION with user_approval_ticket_id (V7 §12.2)
  - Factory binds tenant_id correctly (closure not leaking)
  - E2E: service.transition() with real emitter → Row constructed
  - Schema sanity: V7 §15 columns present
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from kun.core.orm import LifecycleTransitionRow
from kun.governance.capability_lifecycle import (
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
    LifecycleTransition,
)
from kun.integration.capability_lifecycle_db import (
    make_lifecycle_transition_emitter,
    write_lifecycle_transition,
)

# ============================================================
# Fake session helper
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


def _make_transition(
    *,
    transition_id: str = "lct-test-1",
    capability_id: str = "cap-foo",
    from_stage: CapabilityLifecycleStage = CapabilityLifecycleStage.OBSERVATION,
    to_stage: CapabilityLifecycleStage = CapabilityLifecycleStage.CANDIDATE,
    decision_rationale: str = "test rationale",
    user_approval_ticket_id: str | None = None,
    evidence_refs: list[str] | None = None,
    metrics_snapshot: dict[str, Any] | None = None,
) -> LifecycleTransition:
    return LifecycleTransition(
        transition_id=transition_id,
        capability_id=capability_id,
        from_stage=from_stage,
        to_stage=to_stage,
        decided_at=datetime.now(UTC),
        decision_rationale=decision_rationale,
        user_approval_ticket_id=user_approval_ticket_id,
        evidence_refs=list(evidence_refs or []),
        metrics_snapshot=dict(metrics_snapshot or {}),
    )


# ============================================================
# write_lifecycle_transition — happy paths
# ============================================================


@pytest.mark.parametrize(
    "from_stage,to_stage",
    [
        (CapabilityLifecycleStage.OBSERVATION, CapabilityLifecycleStage.CANDIDATE),
        (CapabilityLifecycleStage.REPLAY, CapabilityLifecycleStage.HOLDOUT),
        (CapabilityLifecycleStage.HOLDOUT, CapabilityLifecycleStage.SHADOW),
        (CapabilityLifecycleStage.SHADOW, CapabilityLifecycleStage.CANARY),
        (CapabilityLifecycleStage.PRODUCTION, CapabilityLifecycleStage.MONITOR),
        (CapabilityLifecycleStage.MONITOR, CapabilityLifecycleStage.ROLLBACK),
        (CapabilityLifecycleStage.ROLLBACK, CapabilityLifecycleStage.RETIRE),
    ],
)
async def test_write_transition_stage_combinations(
    monkeypatch: pytest.MonkeyPatch,
    from_stage: CapabilityLifecycleStage,
    to_stage: CapabilityLifecycleStage,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    # For replay target, supply 1 evidence ref to satisfy DB invariant
    evidence = ["strategy_replay_report:rr-x"] if to_stage == CapabilityLifecycleStage.REPLAY else []
    # For production target, supply approval ticket
    approval = "tk-001" if to_stage == CapabilityLifecycleStage.PRODUCTION else None

    transition = _make_transition(
        from_stage=from_stage,
        to_stage=to_stage,
        evidence_refs=evidence,
        user_approval_ticket_id=approval,
    )

    returned_id = await write_lifecycle_transition(
        tenant_id="tenant-a", transition=transition
    )

    assert returned_id == transition.transition_id
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, LifecycleTransitionRow)
    assert row.tenant_id == "tenant-a"
    assert row.transition_id == transition.transition_id
    assert row.from_stage == from_stage.value
    assert row.to_stage == to_stage.value
    assert row.capability_id == "cap-foo"
    assert scope_calls == [{"tenant_id": "tenant-a"}]


async def test_write_candidate_to_replay_with_three_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V7 §12.3 三证据 (writer 透传, 不强校验 — service 层管)."""
    added, _ = _install_fake_session(monkeypatch)
    evidence = [
        "strategy_replay_report:rr-1",
        "process_audit:pa-1",
        "capability_candidate:cc-1",
    ]

    transition = _make_transition(
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=evidence,
    )

    await write_lifecycle_transition(tenant_id="tenant-a", transition=transition)

    row = added[0]
    assert row.evidence_refs == evidence


async def test_write_canary_to_production_with_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V7 §12.2 production flip 必须 user_approval_ticket_id."""
    added, _ = _install_fake_session(monkeypatch)

    transition = _make_transition(
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-prod-flip-001",
    )

    await write_lifecycle_transition(tenant_id="tenant-a", transition=transition)

    row = added[0]
    assert row.to_stage == "production"
    assert row.user_approval_ticket_id == "tk-prod-flip-001"


async def test_write_transition_persists_metrics_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    metrics = {
        "baseline_score": 0.78,
        "candidate_score": 0.85,
        "delta_pct": 8.97,
        "replay_trace_count": 142,
    }

    transition = _make_transition(metrics_snapshot=metrics)

    await write_lifecycle_transition(tenant_id="tenant-b", transition=transition)

    row = added[0]
    assert row.metrics_snapshot == metrics


async def test_write_transition_decision_rationale_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    rationale = "Holdout test 200 traces, candidate +4.2% vs baseline, no regression"
    transition = _make_transition(decision_rationale=rationale)

    await write_lifecycle_transition(tenant_id="tenant-c", transition=transition)

    assert added[0].decision_rationale == rationale


# ============================================================
# Factory — emitter callable + tenant_id binding
# ============================================================


async def test_make_lifecycle_transition_emitter_binds_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    emitter = make_lifecycle_transition_emitter("tenant-emitter-lc")
    transition = _make_transition()
    await emitter(transition)

    assert len(added) == 1
    assert scope_calls == [{"tenant_id": "tenant-emitter-lc"}]


async def test_emitters_are_independent_across_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two emitters bound to different tenants don't share state."""
    added, scope_calls = _install_fake_session(monkeypatch)

    emitter_a = make_lifecycle_transition_emitter("tenant-a")
    emitter_b = make_lifecycle_transition_emitter("tenant-b")

    await emitter_a(_make_transition(transition_id="lct-a"))
    await emitter_b(_make_transition(transition_id="lct-b"))

    assert len(added) == 2
    assert scope_calls == [
        {"tenant_id": "tenant-a"},
        {"tenant_id": "tenant-b"},
    ]
    assert {added[0].tenant_id, added[1].tenant_id} == {"tenant-a", "tenant-b"}


# ============================================================
# E2E — CapabilityLifecycleService actually uses emitter
# ============================================================


async def test_service_transition_writes_to_db_via_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: service with real DB emitter, run transition, confirm Row added."""
    added, scope_calls = _install_fake_session(monkeypatch)

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter("tenant-e2e"),
    )

    record = await service.transition(
        capability_id="cap-e2e",
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-1",
            "process_audit:pa-1",
            "capability_candidate:cc-1",
        ],
        decision_rationale="3 evidence in, ready to replay",
        metrics_snapshot={"replay_planned_traces": 500},
    )

    # Service computed a record
    assert record.from_stage == CapabilityLifecycleStage.CANDIDATE
    assert record.to_stage == CapabilityLifecycleStage.REPLAY
    # Emitter actually wrote
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, LifecycleTransitionRow)
    assert row.tenant_id == "tenant-e2e"
    assert row.capability_id == "cap-e2e"
    assert row.from_stage == "candidate"
    assert row.to_stage == "replay"
    assert scope_calls == [{"tenant_id": "tenant-e2e"}]


async def test_service_production_transition_writes_with_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter("tenant-prod"),
    )

    record = await service.transition(
        capability_id="cap-prod",
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-prod-001",
        evidence_refs=["canary_metrics:cm-1"],
        decision_rationale="Canary 10% × 3 days, no regression, user approved",
    )

    assert record.user_approval_ticket_id == "tk-prod-001"
    assert len(added) == 1
    row = added[0]
    assert row.to_stage == "production"
    assert row.user_approval_ticket_id == "tk-prod-001"


# ============================================================
# Schema sanity — Row exposes V7 §15 columns
# ============================================================


def test_lifecycle_transition_row_has_v7_schema_columns() -> None:
    cols = {c.name for c in LifecycleTransitionRow.__table__.columns}
    expected = {
        "tenant_id",
        "transition_id",
        "capability_id",
        "from_stage",
        "to_stage",
        "decided_at",
        "decision_rationale",
        "user_approval_ticket_id",
        "evidence_refs",
        "metrics_snapshot",
        "created_at",
    }
    assert expected.issubset(cols), (
        f"LifecycleTransitionRow 缺列: {expected - cols}"
    )
