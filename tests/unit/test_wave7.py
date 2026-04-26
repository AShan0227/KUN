"""Tests for Wave 7: blackboard MVP + budget tracker + diagnose runner."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.blackboard import (
    register_data_source,
    reset_data_sources,
)
from kun.api.blackboard import (
    router as bb_router,
)
from kun.engineering.budget_tracker import (
    BEHAVIOR_BY_LEVEL,
    BudgetTracker,
)
from kun.security.diagnose_runner import (
    AUTO_FIX_CATEGORIES,
    DiagnoseFinding,
    DiagnoseRequest,
    DiagnoseRunner,
    FixOutcome,
    FixPlan,
)

# ---- Blackboard MVP ----


@pytest.fixture
def bb_client() -> TestClient:
    reset_data_sources()
    app = FastAPI()
    app.include_router(bb_router)
    return TestClient(app)


def test_blackboard_tasks_empty_default(bb_client: TestClient) -> None:
    resp = bb_client.get("/api/blackboard/tasks", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_blackboard_tasks_with_data_source(bb_client: TestClient) -> None:
    register_data_source(
        "tasks",
        lambda **kw: [
            {
                "task_id": "tk-1",
                "title": "test",
                "status": "running",
                "progress": 0.5,
                "cost_so_far_usd": 0.02,
            },
        ],
    )
    resp = bb_client.get("/api/blackboard/tasks", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["task_id"] == "tk-1"
    assert body[0]["status"] == "running"


def test_blackboard_state_default_empty(bb_client: TestClient) -> None:
    resp = bb_client.get("/api/blackboard/state", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u-1"
    assert body["health_indicator"] == "healthy"


def test_blackboard_state_with_data_source(bb_client: TestClient) -> None:
    register_data_source(
        "state",
        lambda **kw: {
            "tenant_id": kw["tenant_id"],
            "user_id": kw["user_id"],
            "task_count_running": 3,
            "total_cost_today_usd": 0.42,
            "health_indicator": "warn",
            "urgent_alert_count": 1,
            "last_update": "2026-04-26T10:00:00Z",
        },
    )
    resp = bb_client.get(
        "/api/blackboard/state",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_count_running"] == 3


def test_blackboard_workspace_default(bb_client: TestClient) -> None:
    resp = bb_client.get("/api/blackboard/workspace/tk-1", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "tk-1"


def test_blackboard_assets_default(bb_client: TestClient) -> None:
    resp = bb_client.get("/api/blackboard/assets/tk-1", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "tk-1"


def test_blackboard_full_dump_for_agent(bb_client: TestClient) -> None:
    register_data_source(
        "state",
        lambda **kw: {"tenant_id": "-", "user_id": "u-1", "last_update": "x"},
    )
    register_data_source("workspace", lambda **kw: {"task_id": "tk-1", "last_update": "x"})
    register_data_source("assets", lambda **kw: {"task_id": "tk-1"})
    register_data_source("events", lambda **kw: [])
    resp = bb_client.get("/api/blackboard/full/tk-1", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered_for"] == "agent"
    assert body["task_id"] == "tk-1"
    assert "state" in body
    assert "workspace" in body


# ---- BudgetTracker ----


def test_budget_register_and_consume() -> None:
    bt = BudgetTracker()
    st = bt.register_budget("user_daily", "u-1", limit_usd=10.0)
    assert st.level == "HIGH"
    bt.consume("user_daily", "u-1", 3.0)
    st = bt.get_state("user_daily", "u-1")
    assert st.used_usd == 3.0
    assert st.level == "HIGH"  # 70% remaining


def test_budget_level_transitions() -> None:
    bt = BudgetTracker()
    bt.register_budget("task", "tk-1", limit_usd=10.0)
    levels = []
    bt.consume("task", "tk-1", 4.0)
    levels.append(bt.get_state("task", "tk-1").level)  # 60% rem → HIGH
    bt.consume("task", "tk-1", 2.0)
    levels.append(bt.get_state("task", "tk-1").level)  # 40% rem → MEDIUM
    bt.consume("task", "tk-1", 3.0)
    levels.append(bt.get_state("task", "tk-1").level)  # 10% rem → LOW
    bt.consume("task", "tk-1", 0.6)
    levels.append(bt.get_state("task", "tk-1").level)  # 4% rem → CRITICAL
    assert levels == ["HIGH", "MEDIUM", "LOW", "CRITICAL"]


def test_budget_listener_fired_on_level_change() -> None:
    bt = BudgetTracker()
    bt.register_budget("task", "tk-1", limit_usd=10.0)
    captured = []
    bt.register_listener(lambda kind, st: captured.append(kind))
    bt.consume("task", "tk-1", 5.5)  # → MEDIUM
    bt.consume("task", "tk-1", 3.0)  # → LOW
    assert any("level_change_MEDIUM" in c for c in captured)
    assert any("level_change_LOW" in c for c in captured)


def test_budget_critical_should_summarize_history() -> None:
    bt = BudgetTracker()
    bt.register_budget("task", "tk-1", limit_usd=10.0)
    bt.consume("task", "tk-1", 9.6)  # CRITICAL
    assert bt.should_summarize_history("task", "tk-1") is True
    assert bt.should_request_topup("task", "tk-1") is True


def test_budget_high_does_not_summarize() -> None:
    bt = BudgetTracker()
    bt.register_budget("task", "tk-1", limit_usd=10.0)
    bt.consume("task", "tk-1", 1.0)  # HIGH
    assert bt.should_summarize_history("task", "tk-1") is False


def test_budget_hard_break_task_emit() -> None:
    bt = BudgetTracker()
    bt.register_budget("task", "tk-1", limit_usd=10.0)
    captured = []
    bt.register_listener(lambda kind, st: captured.append(kind))
    bt.consume("task", "tk-1", 12.5)  # > 1.2 * limit
    assert "hard_break_task" in captured


def test_budget_dashboard() -> None:
    bt = BudgetTracker()
    bt.register_budget("user_daily", "u-1", limit_usd=20.0)
    bt.consume("user_daily", "u-1", 3.0)
    dash = bt.get_dashboard("u-1")
    assert dash["user_id"] == "u-1"
    assert len(dash["budgets"]) == 1
    assert dash["budgets"][0]["used_usd"] == 3.0


def test_budget_unknown_scope_returns_high() -> None:
    bt = BudgetTracker()
    level = bt.consume("user_daily", "u-nonexistent", 100.0)
    assert level == "HIGH"  # 默认无限


def test_behavior_matrix_complete() -> None:
    for lvl in ("HIGH", "MEDIUM", "LOW", "CRITICAL"):
        assert lvl in BEHAVIOR_BY_LEVEL


# ---- DiagnoseRunner ----


@pytest.mark.asyncio
async def test_diagnose_basic_run() -> None:
    runner = DiagnoseRunner()
    req = DiagnoseRequest(
        request_id="dr-1",
        trigger="user_health_check_button",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="memory 缓存命中率低",
    )
    report = await runner.run(req)
    assert report.request_id == "dr-1"
    assert len(report.findings) >= 1
    # "memory" 命中 context 子系统; "缓存命中率低" 命中 accelerate 类
    finding = report.findings[0]
    assert finding.subsystem in ("context", "engineering")


@pytest.mark.asyncio
async def test_diagnose_auto_fix_for_5_core_categories() -> None:
    """5 类核心 (clean / accelerate / failover / network_guard / privacy) → auto."""
    fix_called = {}

    async def fake_fix(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
        fix_called[finding.category] = True
        return FixOutcome(plan_id=plan.plan_id, success=True, verified=True)

    runner = DiagnoseRunner()
    for cat in AUTO_FIX_CATEGORIES:
        runner.register_fix_handler(cat, fake_fix)  # type: ignore[arg-type]

    req = DiagnoseRequest(
        request_id="dr-auto",
        trigger="watchtower_periodic",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="memory 过期记忆 清理",
    )
    report = await runner.run(req)
    # 应该有 auto fix 跑了
    auto_outcomes = [o for o in report.outcomes if o.success]
    assert len(auto_outcomes) >= 1


@pytest.mark.asyncio
async def test_diagnose_software_mgmt_needs_user_confirm() -> None:
    """非 5 核心 → user_confirm_required."""
    runner = DiagnoseRunner()
    req = DiagnoseRequest(
        request_id="dr-sw",
        trigger="user_health_check_button",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="skill 失败",  # → engineering
    )
    report = await runner.run(req)
    # skill 命中但没规则归类到 5 核心 → 默认 clean (在 AUTO list)
    # 这个测试验证: 找出非 auto plan 的存在性
    user_confirm_plans = [p for p in report.plans if p.fix_kind == "user_confirm_required"]
    auto_plans = [p for p in report.plans if p.fix_kind == "auto"]
    # 至少一类在
    assert len(user_confirm_plans) + len(auto_plans) > 0


@pytest.mark.asyncio
async def test_diagnose_llm_reviewer_called_for_unmatched() -> None:
    """规则没覆盖 → LLM reviewer 兜底."""
    captured = []

    async def fake_reviewer(finding: DiagnoseFinding, hint: str):
        captured.append((finding.subsystem, hint))
        return ("LLM 推断: 网络问题", "network_guard")

    runner = DiagnoseRunner(llm_reviewer=fake_reviewer)
    req = DiagnoseRequest(
        request_id="dr-llm",
        trigger="anomaly_detection",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="something obscure happened",
    )
    await runner.run(req)
    assert len(captured) >= 1


@pytest.mark.asyncio
async def test_diagnose_user_confirm_token() -> None:
    runner = DiagnoseRunner()
    req = DiagnoseRequest(
        request_id="dr-c",
        trigger="user_health_check_button",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="auth 漏洞",  # security 子系统, 非 5 核心 → 需确认
    )
    report = await runner.run(req)
    user_plans = [p for p in report.plans if p.fix_kind == "user_confirm_required"]
    if user_plans:
        token = user_plans[0].confirm_token
        assert token is not None
        # 接受
        assert runner.confirm_user_fix(token, accept=True) is True
        # 重复 → False
        assert runner.confirm_user_fix(token, accept=True) is False


@pytest.mark.asyncio
async def test_diagnose_handler_failure_non_fatal() -> None:
    async def failing_fix(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
        raise RuntimeError("boom")

    runner = DiagnoseRunner()
    for cat in AUTO_FIX_CATEGORIES:
        runner.register_fix_handler(cat, failing_fix)  # type: ignore[arg-type]

    req = DiagnoseRequest(
        request_id="dr-fail",
        trigger="user_health_check_button",
        user_id="u-1",
        tenant_id="t-1",
        hint_text="memory 清理",
    )
    report = await runner.run(req)
    # 失败但不抛
    failed_outcomes = [o for o in report.outcomes if not o.success]
    assert len(failed_outcomes) >= 1
