"""V7 Phase X.B.MF-1 wiring proof — V6 MissionDirectorRunner真触发V7 emitter.

This is the **production-path-必经 evidence** (per V7 §16.6 attacker audit
findings) that closes P0 反模式 1: "代码写完但 runtime path 不通".

What it proves:
  1. Calling the V6 ``control_plane.MissionDirectorRunner.run(work_item)``
     (the exact class cli.py loads) results in a real
     ``MissionAlignmentReviewRow`` insert via the V7 §9.7 service
     (the class my Phase X.B.MD wrote).
  2. The thread fire-and-forget completes deterministically when sync-mocked.
  3. Bridge env opt-out actually skips the emit.
  4. Bridge thread errors do NOT break the V6 path (V6 runner result still OK).

Without this test the X.B.MD claim "Mission Director 接真 DB" is unverified.
With this test we have **end-to-end proof of production wiring**.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.control_plane import (
    MISSION_DIRECTOR_OWNER,
    ArtifactManifest,
    ArtifactRecord,
    ExecutionContract,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    MissionDirectorModelConfig,
    MissionDirectorRunner,
    TaskPlan,
    WorkingContext,
    WorkItem,
)
from kun.core.orm import MissionAlignmentReviewRow

pytestmark = pytest.mark.integration

# ============================================================
# V6 fixture — minimal "product_development" mission ready for review
# ============================================================


def _build_mission_for_review(cp: InMemoryControlPlane) -> Mission:
    """Build a complete V6 mission state that the V6 MissionDirectorRunner
    can run a "review" work item against. Trimmed from the existing test
    fixture in test_control_plane_mission_director_v6.py."""
    mission = Mission(
        mission_id="msn-v7-bridge-test",
        owner="user",
        objective="Bridge wiring proof: V6 runner triggers V7 emitter.",
        task_type="product_development",
        status="contracted",
        current_plan_version="bridge-v1",
    )
    plan = TaskPlan(
        plan_id="plan-bridge-v1",
        mission_id=mission.mission_id,
        version="bridge-v1",
        objective=mission.objective,
        # NOTE: info_gaps must be empty before submit (TaskPlan.can_contract)
        info_gaps=[],
        acceptance_criteria=["bridge fires correctly"],
        decomposition=["build bridge", "wire runner", "verify e2e"],
        worker_plan=["mission-director supervises"],
        test_plan=["this integration test"],
        rollback_plan=["disable bridge env var"],
        evidence_plan=["row in mission_alignment_reviews"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-bridge-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_project"],
        delivery_contract={"final_player_experience_required": False},
    )
    context = WorkingContext(
        working_context_id="ctx-bridge-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="mission-director",
        scope="bridge wiring test",
        summary="Verify V7 emit fires.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Bridge must fire without breaking V6 path."],
    )
    cp.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )

    # Add minimal delivery artifact + evidence + gate so v6 runner can
    # proceed without gating us back to "needs_plan_change"
    delivery_artifact = ArtifactRecord(
        artifact_id="artifact-bridge-delivery",
        kind="answer",
        path_or_uri="mem://bridge",
        content_hash="hash-bridge",
        created_by="kun-game-production-runner",
        mission_id=mission.mission_id,
        supports=["bridge_test_artifact"],
    )
    evidence_artifact = ArtifactRecord(
        artifact_id="artifact-bridge-evidence",
        kind="test_result",
        path_or_uri="mem://bridge-test",
        content_hash="hash-bridge-test",
        created_by="kun-game-production-runner",
        mission_id=mission.mission_id,
        supports=["internal_test_passed"],
    )
    cp.artifacts[delivery_artifact.artifact_id] = delivery_artifact
    cp.artifacts[evidence_artifact.artifact_id] = evidence_artifact
    manifest = ArtifactManifest(
        manifest_id="manifest-bridge",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=[delivery_artifact.artifact_id, evidence_artifact.artifact_id],
        primary_artifact_ref=delivery_artifact.artifact_id,
        evidence_refs=[evidence_artifact.artifact_id],
        created_by="kun-game-production-runner",
        content_hash="hash-mani",
        supports_delivery=True,
    )
    cp.artifact_manifests[manifest.manifest_id] = manifest
    cp.gate_evaluations["gate-bridge"] = GateEvaluation(
        gate_evaluation_id="gate-bridge",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="work-bridge-test",
        stage="acceptance",
        task_type="product_development",
        rubric_version="bridge-v1",
        metric_pack_version="bridge-v1",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        evidence_refs=[evidence_artifact.artifact_id],
        artifact_refs=[delivery_artifact.artifact_id],
        confidence=0.84,
        next_action="ready_to_deliver",
        next_state="delivering",
        created_by="kun-game-production-runner",
    )
    cp.missions[mission.mission_id] = cp.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    return cp.missions[mission.mission_id]


def _review_work_item(mission: Mission) -> WorkItem:
    return WorkItem(
        work_item_id="work-bridge-review",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "bridge-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review for bridge wiring.",
    )


# ============================================================
# Fake session capture (same shape as Phase X.B integration tests)
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
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


def _install_sync_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_emit_v7_review_async`` with a sync wrapper that uses the
    monkey-patched session_scope (not a thread-local engine), and replace
    the bridge's threading.Thread with one that runs the target inline.

    Why two patches:
      - Bridge's thread_target calls asyncio.run(_emit_v7_review_async(
        use_thread_local_engine=True, ...)). That builds a fresh engine →
        bypasses the fake session_scope this test relies on.
      - Override _emit_v7_review_async to forward to the legacy path
        (use_thread_local_engine=False), which DOES go through session_scope.
    """
    import kun.integration.mission_director_v7_bridge as bridge_mod

    real_emit = bridge_mod._emit_v7_review_async

    async def _patched_emit(**kwargs: Any) -> None:
        kwargs.pop("use_thread_local_engine", None)
        await real_emit(**kwargs, use_thread_local_engine=False)

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge._emit_v7_review_async",
        _patched_emit,
    )

    class _SyncThread:
        def __init__(
            self,
            *,
            target: Any,
            name: str = "",
            daemon: bool = False,
            **_kwargs: Any,
        ) -> None:
            self._target = target
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge.threading.Thread",
        _SyncThread,
    )


# ============================================================
# THE wiring proof: V6 runner.run() → V7 emitter → row added
# ============================================================


def test_v6_runner_run_actually_emits_v7_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**This test is the production-path-必经 evidence.**

    Without this test passing, the claim "V7 §9.7 接到生产链路" is unverified.
    """
    # Bridge must be enabled (default) — make explicit
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")
    _install_sync_thread(monkeypatch)
    added, scope_calls = _install_fake_session(monkeypatch)

    cp = InMemoryControlPlane()
    mission = _build_mission_for_review(cp)
    work_item = _review_work_item(mission)

    # Construct the EXACT runner class that cli.py loads in production
    runner = MissionDirectorRunner(
        control_plane=cp,
        model_config=MissionDirectorModelConfig(
            model_id="bridge-test-md",
            model_tier="top",
            provider="test-provider",
        ),
    )

    # Drive the production sync entry point
    result = runner.run(work_item)

    # V6 path still produces correct result (bridge must not break it)
    assert result.status == "done"
    assert result.artifacts  # V6 artifact built normally

    # **Wiring proof**: V7 emitter真fired, row真added to mission_alignment_reviews
    assert len(added) == 1, (
        f"V7 bridge did NOT emit a MissionAlignmentReviewRow; added: {added}"
    )
    row = added[0]
    assert isinstance(row, MissionAlignmentReviewRow)
    # task_id maps from mission_id
    assert row.task_id == mission.mission_id
    assert row.task_plan_version == mission.current_plan_version
    # tenant_id resolved to default (current_tenant().tenant_id)
    assert row.tenant_id  # non-empty
    # verdict computed (depends on coverage estimator; just assert valid value)
    assert row.verdict in {"ok", "drifting", "off_anchor", "needs_human"}
    # session_scope received the resolved tenant_id
    assert scope_calls[0]["tenant_id"] == row.tenant_id


def test_v6_runner_run_does_not_emit_when_bridge_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env opt-out flag truly skips emit — test the kill-switch."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "false")
    _install_sync_thread(monkeypatch)
    added, _ = _install_fake_session(monkeypatch)

    cp = InMemoryControlPlane()
    mission = _build_mission_for_review(cp)
    work_item = _review_work_item(mission)
    runner = MissionDirectorRunner(control_plane=cp)

    result = runner.run(work_item)

    # V6 result still OK
    assert result.status == "done"
    # V7 emitter did NOT fire
    assert added == []


def test_v6_runner_run_survives_bridge_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge import/call exception must not break V6 runner."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")

    # Stub the bridge entry to raise — V6 path must catch + survive
    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("simulated bridge import failure")

    monkeypatch.setattr(
        "kun.integration.mission_director_v7_bridge.emit_v7_review_for_work_item_sync",
        _boom,
    )

    cp = InMemoryControlPlane()
    mission = _build_mission_for_review(cp)
    work_item = _review_work_item(mission)
    runner = MissionDirectorRunner(control_plane=cp)

    # Should NOT raise
    result = runner.run(work_item)
    assert result.status == "done"


def test_v6_runner_non_review_work_item_does_not_trigger_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """can_run=False (e.g. wrong owner) → bridge does not fire."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "true")
    _install_sync_thread(monkeypatch)
    added, _ = _install_fake_session(monkeypatch)

    cp = InMemoryControlPlane()
    mission = _build_mission_for_review(cp)

    # Work item with wrong owner — V6 runner rejects it before bridge
    work_item = WorkItem(
        work_item_id="work-not-md",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "bridge-v1",
        type="review",
        owner="kun-game-production-runner",  # NOT mission-director
    )
    runner = MissionDirectorRunner(control_plane=cp)

    result = runner.run(work_item)

    # V6 short-circuits to "failed" before reaching the bridge call
    assert result.status == "failed"
    assert added == []


# ============================================================
# Audit angle 1 follow-through: grep证据 production code imports bridge
# ============================================================


def test_production_code_imports_bridge() -> None:
    """Grep proof: kun/control_plane/mission_director.py真import了bridge.

    This is the V7 §16.6 "production-path-必经" check — without this import
    line, the entire wiring is invisible to runtime.
    """
    from pathlib import Path

    md_runner_source = (
        Path(__file__).resolve().parent.parent.parent
        / "kun"
        / "control_plane"
        / "mission_director.py"
    ).read_text(encoding="utf-8")
    assert (
        "from kun.integration.mission_director_v7_bridge import"
        in md_runner_source
    ), (
        "kun/control_plane/mission_director.py does NOT import the V7 bridge — "
        "X.B.MF-1 wiring is broken"
    )
    assert "emit_v7_review_for_work_item_sync(" in md_runner_source, (
        "Bridge function is imported but NOT called — wiring still broken"
    )


# Defense in depth: ensure the patch import path used by other tests stays valid
def test_bridge_module_is_importable() -> None:
    import kun.integration.mission_director_v7_bridge as bridge_mod

    assert hasattr(bridge_mod, "emit_v7_review_for_work_item_sync")
    assert hasattr(bridge_mod, "_estimate_coverage_from_v6_state")
    assert hasattr(bridge_mod, "_bridge_enabled")
