"""V7 Phase X.B.MF-1 — V6→V7 Mission Director bridge unit tests.

Tests the bridge logic in isolation:
  - Coverage estimator from V6 control_plane state
  - Env-var opt-out (KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED)
  - Thread fire-and-forget invokes the async emit path
  - Bridge swallows errors instead of breaking V6 caller

The full e2e (V6 runner.run() → V7 emitter fires) is in
tests/integration/test_v6_to_v7_md_wiring.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.v6 import Mission, TaskPlan, WorkItem
from kun.integration.mission_director_v7_bridge import (
    _bridge_enabled,
    _clamp01,
    _estimate_coverage_from_v6_state,
    emit_v7_review_for_work_item_sync,
)

# ============================================================
# Helpers — build minimal V6 state for tests
# ============================================================


def _make_mission(
    *,
    mission_id: str = "msn-test-1",
    current_plan_version: str | None = "v1",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        owner="mission-director",
        objective="test objective",
        task_type="product_development",
        current_plan_version=current_plan_version,
    )


def _make_work_item(
    *,
    mission_id: str = "msn-test-1",
    work_item_id: str = "work-test-1",
    type_: str = "review",
    status: str = "queued",
) -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        mission_id=mission_id,
        task_plan_version="v1",
        type=type_,  # type: ignore[arg-type]
        owner="mission-director",
        status=status,  # type: ignore[arg-type]
    )


def _make_control_plane_with_work_items(
    *,
    mission: Mission,
    n_total_work_items: int = 5,
    n_closed: int = 2,
    info_gaps: list[str] | None = None,
    evidence_plan: list[str] | None = None,
) -> InMemoryControlPlane:
    """Build an InMemoryControlPlane populated with mission + plan + work_items."""
    from kun.control_plane.store import InMemoryControlPlaneStore

    cp = InMemoryControlPlane(store=InMemoryControlPlaneStore())

    # Register mission
    cp.missions[mission.mission_id] = mission

    # Register a plan if mission has plan version
    if mission.current_plan_version:
        plan = TaskPlan(
            mission_id=mission.mission_id,
            version=mission.current_plan_version,
            objective="test plan",
            info_gaps=list(info_gaps or []),
            evidence_plan=list(evidence_plan or []),
        )
        cp.task_plans[plan.plan_id] = plan

    # Register work items
    closed_remaining = n_closed
    for i in range(n_total_work_items):
        status = "done" if closed_remaining > 0 else "queued"
        if closed_remaining > 0:
            closed_remaining -= 1
        wi = _make_work_item(
            mission_id=mission.mission_id,
            work_item_id=f"work-{i:03d}",
            status=status,
        )
        cp.work_items[wi.work_item_id] = wi

    return cp


# ============================================================
# _clamp01
# ============================================================


def test_clamp01_handles_edge_cases() -> None:
    assert _clamp01(-0.5) == 0.0
    assert _clamp01(0.0) == 0.0
    assert _clamp01(0.5) == 0.5
    assert _clamp01(1.0) == 1.0
    assert _clamp01(1.5) == 1.0


# ============================================================
# _bridge_enabled — env-var opt-out
# ============================================================


def test_bridge_enabled_default_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", raising=False)
    assert _bridge_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_bridge_enabled_env_var_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", value)
    assert _bridge_enabled() is expected


# ============================================================
# _estimate_coverage_from_v6_state
# ============================================================


def test_coverage_estimator_no_work_items_returns_1() -> None:
    """Empty mission → coverage = 1.0 (nothing to be incomplete about)."""
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(
        mission=mission,
        n_total_work_items=0,
        n_closed=0,
    )
    info_gap, decomp, evidence, _ = _estimate_coverage_from_v6_state(
        control_plane=cp, mission=mission, payload={"summary": "test"}
    )
    assert info_gap == 1.0
    assert decomp == 1.0
    assert evidence == 1.0


def test_coverage_estimator_decomposition_ratio() -> None:
    """5 work items, 2 closed → decomp=0.4."""
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(
        mission=mission, n_total_work_items=5, n_closed=2
    )
    _, decomp, _, _ = _estimate_coverage_from_v6_state(
        control_plane=cp, mission=mission, payload={"summary": ""}
    )
    assert decomp == pytest.approx(0.4)


def test_coverage_estimator_info_gap_perfect_when_no_tickets() -> None:
    """Plan has info_gaps but no open tickets → info_gap_coverage=1.0."""
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(
        mission=mission,
        info_gaps=["gap-a", "gap-b", "gap-c"],
    )
    info_gap, _, _, _ = _estimate_coverage_from_v6_state(
        control_plane=cp, mission=mission, payload={"summary": ""}
    )
    assert info_gap == 1.0


def test_coverage_estimator_evidence_against_plan() -> None:
    """Plan has 4 evidence items but no artifacts → evidence_coverage=0.0."""
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(
        mission=mission,
        evidence_plan=["evidence-1", "evidence-2", "evidence-3", "evidence-4"],
    )
    _, _, evidence, _ = _estimate_coverage_from_v6_state(
        control_plane=cp, mission=mission, payload={"summary": ""}
    )
    assert evidence == 0.0


def test_coverage_estimator_findings_include_v6_summary() -> None:
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(mission=mission)
    _, _, _, findings = _estimate_coverage_from_v6_state(
        control_plane=cp,
        mission=mission,
        payload={"summary": "Mission Director reviewed alignment with success."},
    )
    assert any("v6_summary" in f for f in findings)


def test_coverage_estimator_findings_call_out_open_work_items() -> None:
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(
        mission=mission, n_total_work_items=5, n_closed=1
    )
    _, _, _, findings = _estimate_coverage_from_v6_state(
        control_plane=cp, mission=mission, payload={"summary": ""}
    )
    assert any("1/5 closed" in f for f in findings)


# ============================================================
# emit_v7_review_for_work_item_sync — env-disabled = noop
# ============================================================


def test_emit_noop_when_bridge_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED=false → no thread spawned."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "false")
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(mission=mission)
    wi = _make_work_item()
    with patch("threading.Thread") as mock_thread:
        emit_v7_review_for_work_item_sync(
            control_plane=cp,
            mission=mission,
            work_item=wi,
            payload={"summary": "test"},
        )
        mock_thread.assert_not_called()


# ============================================================
# emit_v7_review_for_work_item_sync — happy path spawns thread
# ============================================================


def test_emit_spawns_thread_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(mission=mission)
    wi = _make_work_item()

    captured_starts: list[Any] = []

    class _FakeThread:
        def __init__(self, *, target: Any, name: str = "", daemon: bool = False) -> None:
            self._target = target
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            captured_starts.append(self.name)
            # Run target synchronously so we know it completed without error
            self._target()

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge.threading.Thread",
        _FakeThread,
    )

    # Fake session_scope so asyncio.run inside thread target doesn't try real DB
    async def _ok_emit(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge._emit_v7_review_async",
        _ok_emit,
    )

    emit_v7_review_for_work_item_sync(
        control_plane=cp,
        mission=mission,
        work_item=wi,
        payload={"summary": "test"},
    )
    assert captured_starts, "thread.start() was never called"
    assert captured_starts[0].startswith("v7-md-bridge-")


# ============================================================
# emit_v7_review_for_work_item_sync — bridge errors don't propagate
# ============================================================


def test_emit_does_not_raise_when_coverage_estimator_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage estimator raise → bridge logs + swallows, no exception propagated."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(mission=mission)
    wi = _make_work_item()

    def _fail_estimate(**kwargs: Any) -> None:
        raise RuntimeError("simulated estimator failure")

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge._estimate_coverage_from_v6_state",
        _fail_estimate,
    )

    # Should NOT raise — V6 path must survive bridge failure
    emit_v7_review_for_work_item_sync(
        control_plane=cp,
        mission=mission,
        work_item=wi,
        payload={"summary": "test"},
    )


def test_emit_does_not_raise_when_async_thread_target_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async emit raise inside thread → thread logs + swallows."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")
    mission = _make_mission()
    cp = _make_control_plane_with_work_items(mission=mission)
    wi = _make_work_item()

    class _SyncFakeThread:
        def __init__(self, *, target: Any, name: str = "", daemon: bool = False) -> None:
            self._target = target

        def start(self) -> None:
            # Run the target synchronously; bridge should swallow inner failure
            self._target()

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge.threading.Thread",
        _SyncFakeThread,
    )

    async def _bad_emit(**kwargs: Any) -> None:
        raise RuntimeError("simulated async failure")

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge._emit_v7_review_async",
        _bad_emit,
    )

    # No raise expected
    emit_v7_review_for_work_item_sync(
        control_plane=cp,
        mission=mission,
        work_item=wi,
        payload={"summary": "test"},
    )


# ============================================================
# _emit_v7_review_async — DB path with fake session
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
        return None


async def test_emit_v7_review_async_writes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async inner emit really constructs MissionAlignmentReviewRow."""
    from kun.core.orm import MissionAlignmentReviewRow
    from kun.integration.mission_director_v7_bridge import _emit_v7_review_async

    added: list[Any] = []

    @asynccontextmanager
    async def fake_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)

    await _emit_v7_review_async(
        tenant_id="tenant-bridge-async",
        task_id="msn-bridge",
        task_plan_version="v3",
        info_gap_coverage=0.8,
        decomposition_coverage=0.6,
        evidence_coverage=0.5,
        observed_findings=["finding-1"],
    )

    assert len(added) == 1
    row = added[0]
    assert isinstance(row, MissionAlignmentReviewRow)
    assert row.tenant_id == "tenant-bridge-async"
    assert row.task_id == "msn-bridge"
    assert row.task_plan_version == "v3"


async def test_emit_v7_review_async_resolves_default_tenant_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing tenant_id=None falls back to current_tenant().tenant_id."""
    added: list[Any] = []

    @asynccontextmanager
    async def fake_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)

    from kun.integration.mission_director_v7_bridge import _emit_v7_review_async

    await _emit_v7_review_async(
        tenant_id=None,
        task_id="msn-default",
        task_plan_version="v0",
        info_gap_coverage=1.0,
        decomposition_coverage=1.0,
        evidence_coverage=1.0,
        observed_findings=[],
    )

    assert len(added) == 1
    # tenant_id should be resolved to default (non-empty string)
    assert added[0].tenant_id
    assert isinstance(added[0].tenant_id, str)
