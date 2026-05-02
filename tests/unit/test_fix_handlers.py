"""5 类傩诊断 fix handler 单测 (V2.1 §10.6 / M3.2)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.diagnose import router as diag_router
from kun.api.runtime import get_diagnose_runner, install_runtime
from kun.security.diagnose_runner import (
    DiagnoseFinding,
    DiagnoseRequest,
    DiagnoseRunner,
    FixOutcome,
    FixPlan,
)
from kun.security.fix_handlers import (
    accelerate_handler,
    clean_handler,
    failover_handler,
    get_cache_ttl_boost,
    get_fix_audit_log,
    is_task_forced_fallback,
    is_user_throttled,
    network_guard_handler,
    privacy_handler,
    register_default_fix_handlers,
    reset_fix_state,
)
from kun.watchtower.engine import RuleEngine


@pytest.fixture(autouse=True)
def _reset_fix_state():
    reset_fix_state()
    yield
    reset_fix_state()


def _make_finding(category: str, description: str = "test") -> DiagnoseFinding:
    return DiagnoseFinding(
        finding_id="diag-test-1",
        subsystem="engineering",
        category=category,  # type: ignore[arg-type]
        severity="warn",
        description=description,
    )


def _make_plan() -> FixPlan:
    return FixPlan(
        plan_id="diag-plan-1",
        target_finding_id="diag-test-1",
        fix_kind="auto",
        description="test",
    )


# ---- 单 handler 行为 ----


@pytest.mark.asyncio
async def test_clean_handler_short_term() -> None:
    f = _make_finding("clean", "短期记忆过期")
    out = await clean_handler(_make_plan(), f)
    assert out.success is True
    assert out.verified is True
    assert "short_term" in out.notes
    log = get_fix_audit_log()
    assert len(log) == 1
    assert log[0]["category"] == "clean"


@pytest.mark.asyncio
async def test_clean_handler_long_term() -> None:
    f = _make_finding("clean", "长期记忆超阈值")
    out = await clean_handler(_make_plan(), f)
    assert "long_term" in out.notes


@pytest.mark.asyncio
async def test_clean_handler_default_short() -> None:
    f = _make_finding("clean", "未指明 tier")
    out = await clean_handler(_make_plan(), f)
    assert "short_term" in out.notes


@pytest.mark.asyncio
async def test_accelerate_handler_boosts_ttl() -> None:
    f = _make_finding("accelerate", "缓存命中率低")
    out = await accelerate_handler(_make_plan(), f)
    assert out.success is True
    boost = get_cache_ttl_boost(f"finding:{f.finding_id}")
    assert boost == 900


@pytest.mark.asyncio
async def test_failover_handler_marks_task() -> None:
    f = _make_finding("failover", "LLM provider 失败")
    out = await failover_handler(_make_plan(), f)
    assert out.success is True
    assert is_task_forced_fallback(f.finding_id)


@pytest.mark.asyncio
async def test_network_guard_extracts_user_id() -> None:
    f = _make_finding("network_guard", "user u-evil 异常调用模式")
    out = await network_guard_handler(_make_plan(), f)
    assert out.success is True
    assert is_user_throttled("u-evil")


@pytest.mark.asyncio
async def test_network_guard_falls_back_to_finding_id() -> None:
    f = _make_finding("network_guard", "no user mention")
    out = await network_guard_handler(_make_plan(), f)
    assert out.success is True
    assert is_user_throttled(f.finding_id)


@pytest.mark.asyncio
async def test_privacy_handler_purges_user() -> None:
    f = _make_finding("privacy", "user u-leaky 数据足迹超阈值")
    out = await privacy_handler(_make_plan(), f)
    assert out.success is True
    assert out.verified is True


@pytest.mark.asyncio
async def test_audit_log_captures_each_action() -> None:
    f = _make_finding("clean", "短期")
    await clean_handler(_make_plan(), f)
    await accelerate_handler(_make_plan(), _make_finding("accelerate"))
    await failover_handler(_make_plan(), _make_finding("failover"))
    log = get_fix_audit_log()
    assert len(log) == 3
    cats = [e["category"] for e in log]
    assert cats == ["clean", "accelerate", "failover"]


# ---- DiagnoseRunner + 5 类 handler 集成 ----


@pytest.mark.asyncio
async def test_diagnose_runner_runs_with_5_handlers() -> None:
    from kun.security.diagnose_runner import DiagnoseRunner

    runner = DiagnoseRunner()
    register_default_fix_handlers(runner)
    req = DiagnoseRequest(
        request_id="diag-req-1",
        trigger="user_health_check_button",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="过期记忆",  # 命中 RULE_BASED_CAUSES → category=clean
    )
    report = await runner.run(req)
    assert len(report.findings) >= 1
    assert len(report.outcomes) >= 1
    assert all(o.success for o in report.outcomes)


# ---- API endpoint ----


@pytest.fixture
def diag_app():
    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    app.include_router(diag_router, prefix="/api/diagnose")
    return app


def test_install_runtime_registers_diagnose_runner_with_5_handlers() -> None:
    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    runner = get_diagnose_runner(app)
    expected = {"clean", "accelerate", "failover", "network_guard", "privacy"}
    assert expected.issubset(runner._fix_handlers.keys())


def test_api_diagnose_run_returns_report(diag_app) -> None:
    client = TestClient(diag_app)
    resp = client.post(
        "/api/diagnose/run",
        json={"trigger": "user_health_check_button", "hint_text": "过期记忆"},
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "request_id" in body
    assert len(body["findings"]) >= 1
    assert len(body["outcomes"]) >= 1
    assert body["outcomes"][0]["success"] is True


def test_api_diagnose_audit_log_after_run(diag_app) -> None:
    client = TestClient(diag_app)
    client.post(
        "/api/diagnose/run",
        json={"trigger": "user_health_check_button", "hint_text": "缓存命中率低"},
        headers={"X-User-Id": "u-1"},
    )
    resp = client.get("/api/diagnose/audit-log", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1


def test_api_diagnose_confirm_unknown_token_404(diag_app) -> None:
    client = TestClient(diag_app)
    resp = client.post(
        "/api/diagnose/confirm",
        json={"confirm_token": "BOGUS", "accept": True},
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_confirm_user_fix_result_distinguishes_recorded_from_executed() -> None:
    runner = DiagnoseRunner()
    finding = _make_finding("software_mgmt", "需要安装外部软件")
    plan = FixPlan(
        plan_id="diag-plan-confirm",
        target_finding_id=finding.finding_id,
        fix_kind="user_confirm_required",
        description="需要用户确认",
        confirm_token="TOK123",
    )
    runner._pending_confirms["TOK123"] = (plan, finding)

    result = await runner.confirm_user_fix_with_result("TOK123", accept=True)

    assert result.accepted is True
    assert result.executed is False
    assert "没有注册真实执行器" in result.message


@pytest.mark.asyncio
async def test_confirm_user_fix_result_executes_registered_handler() -> None:
    async def handler(plan: FixPlan, _finding: DiagnoseFinding) -> FixOutcome:
        return FixOutcome(plan_id=plan.plan_id, success=True, verified=True, notes="done")

    runner = DiagnoseRunner()
    runner.register_fix_handler("software_mgmt", handler)
    finding = _make_finding("software_mgmt", "需要安装外部软件")
    plan = FixPlan(
        plan_id="diag-plan-exec",
        target_finding_id=finding.finding_id,
        fix_kind="user_confirm_required",
        description="需要用户确认",
        confirm_token="TOK456",
    )
    runner._pending_confirms["TOK456"] = (plan, finding)

    result = await runner.confirm_user_fix_with_result("TOK456", accept=True)

    assert result.accepted is True
    assert result.executed is True
    assert result.outcome is not None
    assert result.outcome.success is True


def test_api_diagnose_confirm_reports_no_executor_truthfully(diag_app) -> None:
    runner = get_diagnose_runner(diag_app)
    finding = _make_finding("software_mgmt", "需要安装外部软件")
    plan = FixPlan(
        plan_id="diag-plan-api-confirm",
        target_finding_id=finding.finding_id,
        fix_kind="user_confirm_required",
        description="需要用户确认",
        confirm_token="TOK789",
    )
    runner._pending_confirms["TOK789"] = (plan, finding)

    client = TestClient(diag_app)
    resp = client.post(
        "/api/diagnose/confirm",
        json={"confirm_token": "TOK789", "accept": True},
        headers={"X-User-Id": "u-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["executed"] is False
    assert body["plan_id"] == "diag-plan-api-confirm"
    assert "不会假装已经修复" in body["message"]
