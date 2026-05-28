"""V7 Phase E — cockpit API 单测 (最小可行版).

测试 Phase E.A endpoint schema 正确, response 含 V7 章节引用.
真实 DB 接入是 Phase E.B 工作.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.cockpit import router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.unit
def test_cockpit_health(client: TestClient) -> None:
    resp = client.get("/cockpit/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "V7 Phase E.A" in data["phase"]
    assert "v7_doc_ref" in data


@pytest.mark.unit
def test_list_capabilities_returns_schema(client: TestClient) -> None:
    resp = client.get("/cockpit/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert "capabilities" in data
    assert "total" in data
    assert "note" in data
    assert "V7" in data["note"]


@pytest.mark.unit
def test_get_capability_stub_returns_501(client: TestClient) -> None:
    resp = client.get("/cockpit/capabilities/cap-123")
    assert resp.status_code == 501
    assert "V7 Phase E.A stub" in resp.json()["detail"]


@pytest.mark.unit
def test_mission_alignment_returns_v7_reference(client: TestClient) -> None:
    resp = client.get("/cockpit/missions/tk-001/alignment")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "tk-001"
    assert "V7 §9.7" in data["note"]


@pytest.mark.unit
def test_rsi_trifecta_returns_three_lines(client: TestClient) -> None:
    """V7 §12.4 RSI 三线: 过去线 + 现在线 + 未来线."""
    resp = client.get("/cockpit/missions/tk-001/rsi-trifecta")
    assert resp.status_code == 200
    data = resp.json()
    assert "trifecta" in data
    assert "past_line" in data["trifecta"]
    assert "present_line" in data["trifecta"]
    assert "future_line" in data["trifecta"]
    # 过去线 = 启 (Qi) post-hoc
    assert "启" in data["trifecta"]["past_line"]["note"]
    # 现在线 = 外部监督者 watchdog
    assert "外部监督者" in data["trifecta"]["present_line"]["note"]
    # 未来线 = 启 (Qi) Explorer Pool
    assert "Explorer Pool" in data["trifecta"]["future_line"]["note"]


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


@pytest.mark.unit
def test_auditor_reports_returns_schema(client: TestClient) -> None:
    resp = client.get("/cockpit/supervisor/auditor-reports")
    assert resp.status_code == 200
    data = resp.json()
    assert "auditor_reports" in data
    assert "V7 §16.6" in (
        data.get("note", "")
        + " ".join(data.get("auditor_reports", []) or [])
    ) or "auditor" in data.get("note", "")
