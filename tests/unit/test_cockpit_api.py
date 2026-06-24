"""V7 Phase E + X.B — cockpit API 单测.

Phase E.A 起步是 stub. Phase X.B 切换到真 DB query (replaced).
这里测试 endpoint schema + 把 readers monkeypatch 成 fake, 不实际打 DB.
真 DB query 测试在 test_cockpit_readers.py + integration test.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.cockpit import router

# ============================================================
# Fake readers fixture (avoid touching real DB in unit tests)
# ============================================================


@pytest.fixture
def fake_readers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Monkeypatch the 3 cockpit DB readers with in-memory fakes returning
    ReaderResult (V7 §16.6 MF-6 contract).

    Mutate ``fake_readers["mission_reviews"]`` etc. to set returned rows.
    Mutate ``fake_readers["error_kind_<table>"]`` to simulate DB errors.
    """
    from kun.api.cockpit_readers import ReaderResult

    state: dict[str, list[dict[str, Any]]] = {
        "mission_reviews": [],
        "lifecycle_transitions": [],
        "auditor_reports": [],
    }
    error_kinds: dict[str, str | None] = {
        "mission_reviews": None,
        "lifecycle_transitions": None,
        "auditor_reports": None,
    }
    last_calls: dict[str, dict[str, Any]] = {}

    async def fake_list_mission_reviews(**kwargs: Any) -> ReaderResult:
        last_calls["mission_reviews"] = kwargs
        return ReaderResult(
            rows=list(state["mission_reviews"]),
            error_kind=error_kinds["mission_reviews"],
        )

    async def fake_list_lifecycle_transitions(**kwargs: Any) -> ReaderResult:
        last_calls["lifecycle_transitions"] = kwargs
        return ReaderResult(
            rows=list(state["lifecycle_transitions"]),
            error_kind=error_kinds["lifecycle_transitions"],
        )

    async def fake_list_auditor_reports(**kwargs: Any) -> ReaderResult:
        last_calls["auditor_reports"] = kwargs
        return ReaderResult(
            rows=list(state["auditor_reports"]),
            error_kind=error_kinds["auditor_reports"],
        )

    monkeypatch.setattr(
        "kun.api.cockpit.list_recent_mission_reviews",
        fake_list_mission_reviews,
    )
    monkeypatch.setattr(
        "kun.api.cockpit.list_recent_lifecycle_transitions",
        fake_list_lifecycle_transitions,
    )
    monkeypatch.setattr(
        "kun.api.cockpit.list_recent_auditor_reports",
        fake_list_auditor_reports,
    )

    yield {
        **state,
        "_calls": last_calls,
        "_error_kinds": error_kinds,
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ============================================================
# Health (unchanged)
# ============================================================


@pytest.mark.unit
def test_cockpit_health(client: TestClient) -> None:
    resp = client.get("/cockpit/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "V7 Phase E.A" in data["phase"]
    assert "v7_doc_ref" in data


# ============================================================
# Capabilities — now backed by lifecycle_transitions
# ============================================================


@pytest.mark.unit
def test_list_capabilities_empty(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """Empty DB → 0 capabilities, schema 保留."""
    resp = client.get("/cockpit/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["capabilities"] == []
    assert data["transitions"] == []
    assert data["total_capabilities"] == 0
    assert data["total_transitions"] == 0
    assert "lifecycle_transitions" in data["data_source"]


@pytest.mark.unit
def test_list_capabilities_groups_by_capability_id(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """多 transition 按 capability_id 分组, current_stage = 最新."""
    fake_readers["lifecycle_transitions"][:] = [
        {  # newest first per reader contract (ORDER BY decided_at DESC)
            "transition_id": "lct-3",
            "capability_id": "cap-a",
            "from_stage": "shadow",
            "to_stage": "canary",
            "decided_at": "2026-05-28T10:00:00+00:00",
            "decision_rationale": "",
            "user_approval_ticket_id": None,
            "evidence_refs": [],
            "metrics_snapshot": {},
        },
        {
            "transition_id": "lct-2",
            "capability_id": "cap-a",
            "from_stage": "holdout",
            "to_stage": "shadow",
            "decided_at": "2026-05-27T10:00:00+00:00",
            "decision_rationale": "",
            "user_approval_ticket_id": None,
            "evidence_refs": [],
            "metrics_snapshot": {},
        },
        {
            "transition_id": "lct-1",
            "capability_id": "cap-b",
            "from_stage": "candidate",
            "to_stage": "replay",
            "decided_at": "2026-05-26T10:00:00+00:00",
            "decision_rationale": "",
            "user_approval_ticket_id": None,
            "evidence_refs": ["strategy_replay_report:rr-1"],
            "metrics_snapshot": {},
        },
    ]
    resp = client.get("/cockpit/capabilities?tenant_id=t-a")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_capabilities"] == 2
    assert data["total_transitions"] == 3
    by_id = {c["capability_id"]: c for c in data["capabilities"]}
    assert by_id["cap-a"]["current_stage"] == "canary"  # latest (newest)
    assert by_id["cap-a"]["transitions_count"] == 2
    assert by_id["cap-b"]["current_stage"] == "replay"
    assert by_id["cap-b"]["transitions_count"] == 1
    # tenant_id propagated
    assert fake_readers["_calls"]["lifecycle_transitions"]["tenant_id"] == "t-a"


@pytest.mark.unit
def test_get_capability_returns_404_when_no_history(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    resp = client.get("/cockpit/capabilities/cap-nope")
    assert resp.status_code == 404
    assert "cap-nope" in resp.json()["detail"]


@pytest.mark.unit
def test_get_capability_returns_history_when_present(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    fake_readers["lifecycle_transitions"][:] = [
        {
            "transition_id": "lct-9",
            "capability_id": "cap-foo",
            "from_stage": "canary",
            "to_stage": "production",
            "decided_at": "2026-05-28T08:00:00+00:00",
            "decision_rationale": "Canary OK 3 days",
            "user_approval_ticket_id": "tk-001",
            "evidence_refs": ["canary_metrics:cm-1"],
            "metrics_snapshot": {"baseline_score": 0.78},
        },
    ]
    resp = client.get("/cockpit/capabilities/cap-foo")
    assert resp.status_code == 200
    data = resp.json()
    assert data["capability_id"] == "cap-foo"
    assert data["current_stage"] == "production"
    assert data["total_transitions"] == 1
    # capability_id filter was forwarded to reader
    assert (
        fake_readers["_calls"]["lifecycle_transitions"]["capability_id"]
        == "cap-foo"
    )


# ============================================================
# Mission alignment — backed by mission_alignment_reviews
# ============================================================


@pytest.mark.unit
def test_mission_alignment_empty_returns_v7_schema(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    resp = client.get("/cockpit/missions/tk-001/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "tk-001"
    assert data["reviews"] == []
    assert data["latest"] is None
    assert data["total"] == 0
    assert "V7 §9.7" in data["data_source"]


@pytest.mark.unit
def test_mission_alignment_with_data(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    fake_readers["mission_reviews"][:] = [
        {
            "review_id": "mar-1",
            "task_id": "tk-001",
            "task_plan_version": "v1",
            "reviewed_at": "2026-05-28T09:00:00+00:00",
            "verdict": "ok",
            "alignment_score": 0.85,
            "findings": [],
            "info_gap_coverage": 0.9,
            "decomposition_coverage": 0.85,
            "evidence_coverage": 0.7,
            "plan_change_proposed": False,
            "plan_change_proposal_id": None,
        }
    ]
    resp = client.get("/cockpit/missions/tk-001/alignment?tenant_id=t-x")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["latest"]["verdict"] == "ok"
    assert data["tenant_id"] == "t-x"
    # task_id forwarded to reader
    assert (
        fake_readers["_calls"]["mission_reviews"]["task_id"] == "tk-001"
    )


# ============================================================
# RSI trifecta — still stub (no DB tables yet for trifecta state)
# ============================================================


@pytest.mark.unit
def test_rsi_trifecta_returns_three_lines(client: TestClient) -> None:
    """V7 §12.4 RSI 三线 — X.Q: now reads real checkpoint trace.

    With no checkpoint for tk-001 (or PG unavailable), ticks_fired=0 and
    lines disabled — but the schema + data_source reflect the real reader,
    not a hardcoded stub.
    """
    resp = client.get("/cockpit/missions/tk-001/rsi-trifecta")
    assert resp.status_code == 200
    data = resp.json()
    assert "trifecta" in data
    assert "past_line" in data["trifecta"]
    assert "present_line" in data["trifecta"]
    assert "future_line" in data["trifecta"]
    assert "启" in data["trifecta"]["past_line"]["note"]
    assert "外部监督者" in data["trifecta"]["present_line"]["note"]
    assert "Explorer Pool" in data["trifecta"]["future_line"]["note"]
    # X.Q — real reader fields present (not the old hardcoded stub)
    assert "ticks_fired" in data
    assert "data_source" in data
    assert "trifecta_ticks" in data["data_source"]


# ============================================================
# Ensemble / Discipline — still stub (待 Phase X.B 续接)
# ============================================================


@pytest.mark.unit
def test_ensemble_recent_returns_schema(client: TestClient) -> None:
    resp = client.get("/cockpit/ensemble/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert "ensemble_calls" in data
    assert "total" in data
    assert "limit" in data


@pytest.mark.unit
def test_discipline_recent_returns_schema(client: TestClient) -> None:
    resp = client.get("/cockpit/discipline/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert "discipline_checks" in data


# ============================================================
# Auditor reports — backed by auditor_reports
# ============================================================


@pytest.mark.unit
def test_auditor_reports_empty_returns_v7_schema(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    resp = client.get("/cockpit/supervisor/auditor-reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["auditor_reports"] == []
    assert data["total"] == 0
    assert data["risk_distribution"] == {"P0": 0, "P1": 0, "P2": 0}
    assert data["block_release_count"] == 0
    assert "V7 §16.6" in data["data_source"]


@pytest.mark.unit
def test_auditor_reports_aggregates_risk_distribution(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    fake_readers["auditor_reports"][:] = [
        {
            "report_id": "ar-1",
            "audited_capability": "self-reflect",
            "audited_at": "2026-05-28T09:00:00+00:00",
            "auditor_provider": "anthropic/claude-opus",
            "design_promise": "...",
            "real_code_path": "...",
            "bypass_methods": [],
            "min_repro_steps": "",
            "risk_level": "P0",
            "must_fix": [],
            "acceptance_tests": [],
            "allow_release": False,
            "rationale": "",
        },
        {
            "report_id": "ar-2",
            "audited_capability": "grep-verify",
            "audited_at": "2026-05-27T09:00:00+00:00",
            "auditor_provider": "openai/gpt-5.5",
            "design_promise": "...",
            "real_code_path": "...",
            "bypass_methods": [],
            "min_repro_steps": "",
            "risk_level": "P1",
            "must_fix": [],
            "acceptance_tests": [],
            "allow_release": False,
            "rationale": "",
        },
        {
            "report_id": "ar-3",
            "audited_capability": "lifecycle gate",
            "audited_at": "2026-05-26T09:00:00+00:00",
            "auditor_provider": "anthropic/claude-opus",
            "design_promise": "...",
            "real_code_path": "...",
            "bypass_methods": [],
            "min_repro_steps": "",
            "risk_level": "P2",
            "must_fix": [],
            "acceptance_tests": [],
            "allow_release": True,
            "rationale": "",
        },
    ]
    resp = client.get("/cockpit/supervisor/auditor-reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["risk_distribution"] == {"P0": 1, "P1": 1, "P2": 1}
    assert data["block_release_count"] == 2  # P0 + P1


@pytest.mark.unit
def test_auditor_reports_filter_by_risk_level(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    resp = client.get(
        "/cockpit/supervisor/auditor-reports?risk_level=P0&tenant_id=t-z"
    )
    assert resp.status_code == 200
    # Forwarded to reader as filter
    call_kwargs = fake_readers["_calls"]["auditor_reports"]
    assert call_kwargs["risk_level"] == "P0"
    assert call_kwargs["tenant_id"] == "t-z"


@pytest.mark.unit
def test_auditor_reports_invalid_risk_level_returns_422(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """pattern='^P[0-2]$' 拦截非法 risk_level."""
    resp = client.get("/cockpit/supervisor/auditor-reports?risk_level=P9")
    assert resp.status_code == 422


# ============================================================
# V7 Phase X.B.MF-3 — writes_wired_status field (no fake-success)
# ============================================================


@pytest.mark.unit
def test_capabilities_response_includes_writes_wired_status(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """MF-3 + MF-LC-wiring: /capabilities surfaces writes_wired_status.
    MF-LC-wiring made this default-on (was ORPHAN before)."""
    resp = client.get("/cockpit/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert "writes_wired_status" in data
    status = data["writes_wired_status"]
    # MF-LC-wiring landed — lifecycle_transitions now has production writer
    # via GateService.admit
    assert status["writes_wired"] is True
    assert "GateService.admit" in status["writer"]
    assert "X.B.MF-LC-wiring" in status["writer"]


@pytest.mark.unit
def test_capabilities_writes_wired_false_when_lc_bridge_disabled(
    client: TestClient,
    fake_readers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env kill switch works for MF-LC-wiring bridge."""
    monkeypatch.setenv(
        "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "false"
    )
    resp = client.get("/cockpit/capabilities")
    data = resp.json()
    assert data["writes_wired_status"]["writes_wired"] is False
    assert "Bridge disabled" in (data["writes_wired_status"]["warning"] or "")


@pytest.mark.unit
def test_mission_alignment_response_includes_writes_wired_status(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """MF-3: /missions/{id}/alignment surfaces the X.B.MF-1 bridge status."""
    resp = client.get("/cockpit/missions/tk-x/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert "writes_wired_status" in data
    status = data["writes_wired_status"]
    # X.B.MF-1 wired this — default env-on
    assert status["writes_wired"] is True
    assert "X.B.MF-1" in status["writer"]


@pytest.mark.unit
def test_mission_alignment_writes_wired_false_when_bridge_disabled(
    client: TestClient,
    fake_readers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED=false, status surfaces it."""
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "false")
    resp = client.get("/cockpit/missions/tk-x/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert data["writes_wired_status"]["writes_wired"] is False
    assert "Bridge disabled" in (data.get("warning") or "")


@pytest.mark.unit
def test_auditor_reports_response_includes_writes_wired_status(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """MF-3 + MF-AR-wiring: auditor_reports now has heuristic writer."""
    resp = client.get("/cockpit/supervisor/auditor-reports")
    assert resp.status_code == 200
    data = resp.json()
    status = data["writes_wired_status"]
    # MF-AR-wiring landed — heuristic writer fires on lifecycle transitions
    assert status["writes_wired"] is True
    assert "X.B.MF-AR-wiring" in status["writer"]
    assert "heuristic" in status.get("note", "")


@pytest.mark.unit
def test_auditor_reports_writes_wired_false_when_ar_bridge_disabled(
    client: TestClient,
    fake_readers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env kill switch works for MF-AR-wiring bridge."""
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "false")
    resp = client.get("/cockpit/supervisor/auditor-reports")
    data = resp.json()
    assert data["writes_wired_status"]["writes_wired"] is False
    assert "Bridge disabled" in (data["writes_wired_status"]["warning"] or "")


@pytest.mark.unit
def test_ensemble_endpoint_writes_wired_false_when_env_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default deployment (env unset) → ensemble writes_wired = false."""
    monkeypatch.delenv("KUN_V7_ENSEMBLE_ENABLED", raising=False)
    monkeypatch.delenv("KUN_V7_ENSEMBLE_TIERS", raising=False)
    resp = client.get("/cockpit/ensemble/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["writes_wired_status"]["writes_wired"] is False
    assert "Ensemble disabled" in (data["writes_wired_status"]["warning"] or "")


@pytest.mark.unit
def test_ensemble_endpoint_writes_wired_true_when_env_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env on + ≥ 2 tiers → ensemble writes_wired = true."""
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top,cheap")
    resp = client.get("/cockpit/ensemble/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["writes_wired_status"]["writes_wired"] is True


@pytest.mark.unit
def test_writes_status_meta_endpoint(client: TestClient) -> None:
    """/cockpit/writes-status — meta endpoint listing all table wiring."""
    resp = client.get("/cockpit/writes-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "tables" in data
    tables = data["tables"]
    assert "mission_alignment_reviews" in tables
    assert "ensemble_calls" in tables
    assert "lifecycle_transitions" in tables
    assert "auditor_reports" in tables
    # Each entry must have writes_wired bool + writer description
    for tname, status in tables.items():
        assert isinstance(status["writes_wired"], bool), tname
        # warning is None when wired, str when not
        warning = status.get("warning")
        assert warning is None or isinstance(warning, str)
    # MF progress tracker
    assert "mf_progress" in data
    assert "MF-1" in data["mf_progress"]


# ============================================================
# V7 Phase X.B.MF-6 — reader_error_kind 在 endpoint surface
# ============================================================


@pytest.mark.unit
def test_mission_alignment_surfaces_reader_error_kind(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """When reader DB fails, endpoint reveals error_kind (not silent empty)."""
    fake_readers["_error_kinds"]["mission_reviews"] = "db_connection_refused"
    resp = client.get("/cockpit/missions/tk-x/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reader_error_kind"] == "db_connection_refused"
    assert data["reviews"] == []  # honest empty due to DB failure
    # Combined with writes_wired_status, consumer knows: writer is wired but
    # READ failed — separate concerns


@pytest.mark.unit
def test_mission_alignment_no_reader_error_when_ok(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """Successful DB read (even empty) → reader_error_kind=None."""
    resp = client.get("/cockpit/missions/tk-empty/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reader_error_kind"] is None


@pytest.mark.unit
def test_capabilities_surfaces_reader_error_kind(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    fake_readers["_error_kinds"]["lifecycle_transitions"] = "table_not_found"
    resp = client.get("/cockpit/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reader_error_kind"] == "table_not_found"
    assert data["transitions"] == []


@pytest.mark.unit
def test_get_capability_returns_503_when_reader_error(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """Capability lookup with DB error → 503, not 404 (different problem)."""
    fake_readers["_error_kinds"]["lifecycle_transitions"] = "db_connection_refused"
    resp = client.get("/cockpit/capabilities/cap-x")
    assert resp.status_code == 503
    assert "db_connection_refused" in resp.json()["detail"]


@pytest.mark.unit
def test_get_capability_returns_404_when_truly_no_data(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    """No error + no rows → 404 (tenant truly has no data)."""
    fake_readers["_error_kinds"]["lifecycle_transitions"] = None
    resp = client.get("/cockpit/capabilities/cap-nope")
    assert resp.status_code == 404
    assert "lifecycle 记录" in resp.json()["detail"]


@pytest.mark.unit
def test_auditor_reports_surfaces_reader_error_kind(
    client: TestClient, fake_readers: dict[str, Any]
) -> None:
    fake_readers["_error_kinds"]["auditor_reports"] = "permission_denied"
    resp = client.get("/cockpit/supervisor/auditor-reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reader_error_kind"] == "permission_denied"
