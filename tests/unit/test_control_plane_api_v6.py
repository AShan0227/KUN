from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.control_plane import router
from kun.control_plane import (
    ArtifactRecord,
    CapabilityCandidate,
    CapabilityEvaluation,
    CollaborationTicket,
    DaemonServiceState,
    ExecutionContract,
    FileDaemonServiceStateStore,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    build_capability_promotion,
    build_capability_rollback,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _payload(*, approved: bool = True) -> dict[str, object]:
    mission = Mission(
        mission_id="msn-api-v6",
        owner="customer",
        objective="Deliver a traceable result",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-api-v6",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["Useful and verified."],
        constraints=["No external action without approval."],
        evidence_plan=["Attach evidence and test refs."],
        decomposition=["research"],
        worker_plan=["kun"],
        merge_plan=["single worker"],
        test_plan=["gate"],
        rollback_plan=["repair"],
        approval_status="approved" if approved else "draft",
    )
    contract = ExecutionContract(
        contract_id="contract-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["research"],
        forbidden_actions=["publish_without_approval"],
    )
    context = WorkingContext(
        working_context_id="ctx-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="operator",
        scope="mission",
        summary="Traceable result required.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    item = WorkItem(
        work_item_id="work-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner="kun",
        expected_output="evidence",
    )
    return {
        "mission": mission.model_dump(mode="json"),
        "task_plan": plan.model_dump(mode="json"),
        "execution_contract": contract.model_dump(mode="json"),
        "working_context": context.model_dump(mode="json"),
        "work_items": [item.model_dump(mode="json")],
        "actor": "kun-test",
    }


class WaitingRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "collaboration-test-runner"

    def __init__(self, ticket: CollaborationTicket) -> None:
        self.ticket = ticket

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        return WorkItemResult(
            status="waiting_human",
            summary="Need user approval before continuing.",
            collaboration_tickets=[self.ticket],
        )


def _capability_candidate() -> CapabilityCandidate:
    return CapabilityCandidate(
        candidate_id="cand-api-capability",
        capability_name="Production-only runtime capability",
        source="real_task_review",
        source_ref="review-api-capability",
        hypothesis="Only production-promoted profiles should load by default.",
        target_task_types=["product_development"],
        evidence_refs=["artifact-api-gap"],
    )


def _capability_evaluation(stage: str) -> CapabilityEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": f"eval-api-{stage}",
        "candidate_id": "cand-api-capability",
        "stage": stage,
        "mission_id": "msn-api-capability",
        "task_plan_version": "v6",
        "subject_ref": f"work-api-{stage}",
        "passed": True,
        "result_quality": 0.91,
        "speed": 0.7,
        "cost": 0.7,
        "risk": 0.2,
        "evidence_refs": [f"artifact-api-evidence-{stage}"],
        "artifact_refs": [f"artifact-api-report-{stage}"],
    }
    if stage in {"holdout", "canary", "production"}:
        payload["holdout_refs"] = ["artifact-api-holdout"]
    if stage in {"canary", "production"}:
        payload["regression_refs"] = ["artifact-api-regression"]
        payload["rollback_plan"] = ["disable api capability"]
    return CapabilityEvaluation.model_validate(payload)


def _materialize_api_evidence(
    runtime,
    *,
    mission_id: str,
    artifact_id: str,
    support: str,
    created_by: str = "control-plane",
) -> ArtifactRecord:
    artifact = ArtifactRecord(
        artifact_id=artifact_id,
        kind="review" if "review" in support or "supervision" in support else "report",
        path_or_uri=f"control-plane://api-test/{mission_id}/{artifact_id}",
        content_hash=f"hash-{artifact_id}",
        created_by=created_by,
        mission_id=mission_id,
        supports=[support],
        freshness="fresh",
        source_quality="primary",
    )
    runtime.artifacts[artifact.artifact_id] = artifact
    if runtime.store is not None:
        runtime.store.put_artifact_record(artifact)
    return artifact


@pytest.mark.unit
def test_control_plane_api_submits_and_reports_progress() -> None:
    client = TestClient(_app())

    response = client.post("/api/control-plane/v6/missions", json=_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "queued"

    progress = client.get("/api/control-plane/v6/missions/msn-api-v6/progress")
    assert progress.status_code == 200
    body = progress.json()
    assert body["mission_id"] == "msn-api-v6"
    assert body["status"] == "queued"
    assert body["total_work_items"] == 1
    assert body["next_ready_work_item_ids"] == ["work-api-v6"]
    assert body["ledger_event_count"] == 1

    user_progress = client.get("/api/control-plane/v6/missions/msn-api-v6/progress/user")
    assert user_progress.status_code == 200
    assert user_progress.json()["tone"] == "working"
    assert user_progress.json()["safe_to_continue"] is True

    dashboard = client.get("/api/control-plane/v6/missions/msn-api-v6/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["headline"] == "Mission is moving under Control Plane."

    cockpit = client.get("/api/control-plane/v6/missions/msn-api-v6/cockpit")
    assert cockpit.status_code == 200
    assert cockpit.json()["objective"] == "Deliver a traceable result"
    assert cockpit.json()["progress"]["ready"] == 1
    assert cockpit.json()["quality_gate"]["status"] == "unknown"

    recovery = client.get("/api/control-plane/v6/missions/msn-api-v6/recovery-bundle")
    assert recovery.status_code == 200
    assert recovery.json()["resume_policy"] == "resume_next_ready_work_item"

    productization = client.get("/api/control-plane/v6/missions/msn-api-v6/productization-audit")
    assert productization.status_code == 200
    assert "qi_ab_runner" in productization.json()["missing_subsystems"]

    ready = client.get("/api/control-plane/v6/missions/msn-api-v6/ready-work-item")
    assert ready.status_code == 200
    assert ready.json()["work_item_id"] == "work-api-v6"


@pytest.mark.unit
def test_control_plane_api_cockpit_surfaces_daemon_service_state() -> None:
    app = _app()
    app.state.v6_daemon_service_state = DaemonServiceState(
        daemon_id="daemon-api",
        status="running",
        started_at=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        updated_at=datetime.now(UTC),
        process_id=1234,
        tick_count=2,
        active_mission_ids=["msn-api-v6"],
        last_heartbeat_at=datetime.now(UTC),
    )
    client = TestClient(app)
    response = client.post("/api/control-plane/v6/missions", json=_payload())
    assert response.status_code == 200

    cockpit = client.get("/api/control-plane/v6/missions/msn-api-v6/cockpit")

    assert cockpit.status_code == 200
    assert cockpit.json()["daemon"]["healthy"] is True
    assert cockpit.json()["daemon"]["service_status"] == "running"


@pytest.mark.unit
def test_control_plane_api_persists_and_loads_daemon_service_status(tmp_path) -> None:
    app = _app()
    app.state.v6_daemon_service_state_store = FileDaemonServiceStateStore(
        tmp_path / "daemon-service-state.json"
    )
    client = TestClient(app)
    state = DaemonServiceState(
        daemon_id="daemon-api",
        status="running",
        started_at=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        updated_at=datetime.now(UTC),
        process_id=1234,
        tick_count=3,
        active_mission_ids=["msn-api-v6"],
        last_heartbeat_at=datetime.now(UTC),
    )

    written = client.put(
        "/api/control-plane/v6/daemon-service/status",
        json={"state": state.model_dump(mode="json")},
    )
    assert written.status_code == 200
    assert written.json()["healthy"] is True

    app.state.v6_daemon_service_state = None
    loaded = client.get("/api/control-plane/v6/daemon-service/status")

    assert loaded.status_code == 200
    assert loaded.json()["state"]["daemon_id"] == "daemon-api"
    assert loaded.json()["state"]["tick_count"] == 3
    assert loaded.json()["healthy"] is True


@pytest.mark.unit
def test_control_plane_api_flags_stale_daemon_service_status() -> None:
    app = _app()
    app.state.v6_daemon_service_state = DaemonServiceState(
        daemon_id="daemon-api",
        status="running",
        started_at=datetime(2020, 1, 1, 8, 0, tzinfo=UTC),
        updated_at=datetime(2020, 1, 1, 8, 1, tzinfo=UTC),
        process_id=1234,
        last_heartbeat_at=datetime(2020, 1, 1, 8, 1, tzinfo=UTC),
    )
    client = TestClient(app)

    status = client.get("/api/control-plane/v6/daemon-service/status")

    assert status.status_code == 200
    assert status.json()["healthy"] is False
    assert status.json()["stale"] is True


@pytest.mark.unit
def test_control_plane_api_claims_and_stops_daemon_service(tmp_path) -> None:
    app = _app()
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    app.state.v6_daemon_service_state_store = state_store
    client = TestClient(app)

    claim = client.post(
        "/api/control-plane/v6/daemon-service/start-claim",
        json={
            "daemon_id": "daemon-api",
            "process_id": 4321,
            "config": {"stale_heartbeat_after_sec": 1800},
        },
    )

    assert claim.status_code == 200
    body = claim.json()
    assert body["claim"]["accepted"] is True
    assert body["claim"]["state"]["status"] == "starting"
    assert body["claim"]["state"]["process_id"] == 4321
    assert body["status"]["healthy"] is True

    duplicate = client.post(
        "/api/control-plane/v6/daemon-service/start-claim",
        json={"daemon_id": "daemon-api", "config": {"stale_heartbeat_after_sec": 1800}},
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["claim"]["accepted"] is False

    stop = client.post(
        "/api/control-plane/v6/daemon-service/stop-request",
        json={
            "daemon_id": "daemon-api",
            "requested_by": "operator",
            "reason": "maintenance",
        },
    )

    assert stop.status_code == 200
    assert stop.json()["pending"] is True
    assert state_store.stop_requested(daemon_id="daemon-api") is True


@pytest.mark.unit
def test_control_plane_api_rejects_unapproved_plan() -> None:
    client = TestClient(_app())

    response = client.post("/api/control-plane/v6/missions", json=_payload(approved=False))

    assert response.status_code == 409
    assert "approved" in response.json()["detail"]


@pytest.mark.unit
def test_control_plane_api_records_collaboration_response_and_resumes() -> None:
    client = TestClient(_app())
    submit = client.post("/api/control-plane/v6/missions", json=_payload())
    assert submit.status_code == 200

    ticket = CollaborationTicket(
        ticket_id="collab-api-v6",
        mission_id="msn-api-v6",
        type="approval",
        role_needed="customer",
        why_needed="Approval is required before continuing the long task.",
        decision_options=["approve", "pause"],
        recommended_option="approve",
        context_ref="work-api-v6",
        risk_if_skipped="KUN may continue without the customer's intended boundary.",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        resume_after_response=True,
        output_contract="Select approve or pause.",
    )
    runtime = cast(FastAPI, client.app).state.v6_control_plane
    runtime.run_next_ready(mission_id="msn-api-v6", runner=WaitingRunner(ticket))

    waiting_progress = client.get("/api/control-plane/v6/missions/msn-api-v6/progress")
    assert waiting_progress.status_code == 200
    assert waiting_progress.json()["status"] == "waiting_human"

    tickets = client.get("/api/control-plane/v6/missions/msn-api-v6/collaboration-tickets")
    assert tickets.status_code == 200
    assert tickets.json()[0]["ticket_id"] == "collab-api-v6"

    response = client.post(
        "/api/control-plane/v6/collaboration-tickets/collab-api-v6/response",
        json={
            "response": {
                "ticket_id": "collab-api-v6",
                "responder": "customer",
                "selected_option": "approve",
                "answer": "Go ahead.",
            },
            "actor": "customer",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "answered"

    resumed_progress = client.get("/api/control-plane/v6/missions/msn-api-v6/progress")
    assert resumed_progress.status_code == 200
    body = resumed_progress.json()
    assert body["status"] == "queued"
    assert body["next_ready_work_item_ids"] == ["work-api-v6"]


@pytest.mark.unit
def test_control_plane_api_distills_external_behavior_to_qi_candidates() -> None:
    client = TestClient(_app())

    response = client.post(
        "/api/control-plane/v6/external-behavior/signals",
        json={
            "sources": {
                "external_repos/openclaw/README.md": (
                    "Gateway sessions tools with approval buttons."
                ),
                "external_repos/hermes-agent/RELEASE_v0.8.0.md": (
                    "Behavioral benchmarking with inactivity timeout."
                ),
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["signals"]
    assert body["capability_candidates"]
    assert body["capability_candidates"][0]["source"] == "open_source_project"


@pytest.mark.unit
def test_control_plane_api_productionizes_external_behavior_into_default_runtime() -> None:
    client = TestClient(_app())
    submit = client.post("/api/control-plane/v6/missions", json=_payload())
    assert submit.status_code == 200
    distilled = client.post(
        "/api/control-plane/v6/external-behavior/signals",
        json={
            "sources": {
                "external_repos/openclaw/README.md": (
                    "Gateway sessions tools with multi-agent isolated workspace and approval buttons."
                ),
                "external_repos/hermes-agent/agent/context_compressor.py": (
                    "Background notify with inactivity timeout, behavioral benchmark, and structured logging."
                ),
            }
        },
    )
    assert distilled.status_code == 200
    signals = distilled.json()["signals"]
    runtime = cast(FastAPI, client.app).state.v6_control_plane
    _materialize_api_evidence(
        runtime,
        mission_id="msn-api-v6",
        artifact_id="artifact-api-real-long-task-dogfood",
        support="real_long_task_dogfood",
    )
    _materialize_api_evidence(
        runtime,
        mission_id="msn-api-v6",
        artifact_id="artifact-api-ab-regression",
        support="ab_regression_gate",
    )
    _materialize_api_evidence(
        runtime,
        mission_id="msn-api-v6",
        artifact_id="review-gpt-5.5-external-behavior",
        support="gpt55_supervision_review",
        created_by="gpt-5.5",
    )

    productionized = client.post(
        "/api/control-plane/v6/external-behavior/productionize",
        json={
            "mission_id": "msn-api-v6",
            "signals": signals,
            "dogfood_validation_refs": ["artifact-api-real-long-task-dogfood"],
            "regression_refs": ["artifact-api-ab-regression"],
            "supervisor_review_ref": "review-gpt-5.5-external-behavior",
            "actor": "qi",
        },
    )

    assert productionized.status_code == 200
    body = productionized.json()
    assert body["adopted_count"] >= 1
    assert body["merged_count"] >= 1
    assert body["capability_profile_refs"]
    assert body["supervisor_review_ref"] == "review-gpt-5.5-external-behavior"
    default_profiles = client.get("/api/control-plane/v6/runtime-capabilities/default")
    assert default_profiles.status_code == 200
    assert len(default_profiles.json()) == len(body["capability_profile_refs"])
    assert all(profile["runtime_enabled"] is True for profile in default_profiles.json())


@pytest.mark.unit
def test_control_plane_api_applies_capability_promotion_without_loading_replay_by_default() -> None:
    client = TestClient(_app())
    candidate = _capability_candidate()
    replay_promotion = build_capability_promotion(
        candidate,
        [_capability_evaluation("replay")],
        target_stage="replay",
        capability_id="cap-api-replay",
    )

    replay_response = client.post(
        "/api/control-plane/v6/capability-promotions",
        json={"promotion": replay_promotion.model_dump(mode="json")},
    )

    assert replay_response.status_code == 200
    assert replay_response.json()["default_runtime_enabled"] is False
    assert client.get("/api/control-plane/v6/runtime-capabilities/default").json() == []

    production_promotion = build_capability_promotion(
        candidate,
        [
            _capability_evaluation("replay"),
            _capability_evaluation("holdout"),
            _capability_evaluation("shadow"),
            _capability_evaluation("canary"),
            _capability_evaluation("production"),
        ],
        target_stage="production",
        capability_id="cap-api-production",
    )

    production_response = client.post(
        "/api/control-plane/v6/capability-promotions",
        json={"promotion": production_promotion.model_dump(mode="json")},
    )

    assert production_response.status_code == 200
    assert production_response.json()["default_runtime_enabled"] is True
    default_profiles = client.get("/api/control-plane/v6/runtime-capabilities/default")
    assert default_profiles.status_code == 200
    assert [item["capability_id"] for item in default_profiles.json()] == ["cap-api-production"]

    production_profile = production_promotion.capability_profile
    assert production_profile is not None
    rollback = build_capability_rollback(
        production_profile,
        _capability_evaluation("production").model_copy(
            update={
                "evaluation_id": "eval-api-production-failed",
                "passed": False,
                "result_quality": 0.74,
                "hard_gate_failures": ["production_runtime_regression"],
                "evidence_refs": ["artifact-api-production-failure"],
            }
        ),
        reason="API dogfood regression reduced delivery quality",
    )

    rollback_response = client.post(
        "/api/control-plane/v6/capability-rollbacks",
        json={"rollback": rollback.model_dump(mode="json"), "actor": "qi"},
    )

    assert rollback_response.status_code == 200
    assert rollback_response.json()["default_runtime_enabled"] is False
    assert rollback_response.json()["capability_profile"]["runtime_enabled"] is False
    default_after_rollback = client.get("/api/control-plane/v6/runtime-capabilities/default")
    assert default_after_rollback.status_code == 200
    assert default_after_rollback.json() == []
