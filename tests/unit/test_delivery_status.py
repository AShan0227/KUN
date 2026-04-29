"""Honest delivery status for NUO/KUN."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.nuo.health_panel import router
from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
from kun.engineering.ops_dogfood import (
    dogfood_scenario_report,
    dogfood_scenario_summary,
    get_v3_ops_dogfood_scenarios,
    validate_ops_dogfood_scenarios,
)


def test_delivery_status_is_honest_about_incomplete_capabilities() -> None:
    items = get_v3_delivery_status()

    by_id = {item.capability_id: item for item in items}
    assert by_id["llm_provider"].status == "ready"
    assert by_id["llm_provider"].can_claim_complete is True
    assert by_id["world_gateway"].status == "partial"
    assert by_id["production_deployment"].status == "not_ready"
    assert by_id["world_gateway"].can_claim_complete is False
    assert "local_file.write 可写入受控输出目录" in by_id["world_gateway"].done
    assert validate_delivery_status(items) == []


def test_delivery_status_endpoint() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/delivery-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["ready"] >= 1
    assert body["summary"]["not_ready"] >= 1
    assert body["validation_issues"] == []
    assert any(item["capability_id"] == "world_gateway" for item in body["items"])


def test_ops_dogfood_scenarios_are_honest_and_actionable() -> None:
    scenarios = get_v3_ops_dogfood_scenarios()
    by_id = {item.scenario_id: item for item in scenarios}

    assert by_id["safe_world_action_review"].status == "limited"
    assert by_id["release_ops_smoke"].status == "blocked"
    assert by_id["release_ops_smoke"].blockers
    assert all(item.smoke_command for item in scenarios)
    assert all(item.ready_when for item in scenarios)
    assert validate_ops_dogfood_scenarios(scenarios) == []

    summary = dogfood_scenario_summary(scenarios)
    assert summary["limited"] >= 1
    assert summary["blocked"] >= 1


def test_ops_dogfood_report_endpoint() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/dogfood-scenarios")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["blocked"] >= 1
    assert body["validation_issues"] == []
    assert any(item["scenario_id"] == "release_ops_smoke" for item in body["scenarios"])


def test_ops_dogfood_report_model_roundtrip() -> None:
    report = dogfood_scenario_report()

    assert report.summary == dogfood_scenario_summary(report.scenarios)
    assert report.validation_issues == []
