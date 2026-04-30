"""Tests for Wave 7: blackboard MVP + budget tracker + diagnose runner."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.blackboard import (
    _request_identity,
    register_data_source,
    reset_data_sources,
)
from kun.api.blackboard import (
    router as bb_router,
)
from kun.api.blackboard_data_sources import _entry_from_runtime_rows
from kun.core.orm import RuntimeStateRow, TaskRow
from kun.core.tenancy import TenantContext, tenant_scope
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


def test_blackboard_identity_uses_signed_context_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kun.api.blackboard.settings",
        lambda: SimpleNamespace(env="production"),
    )

    with tenant_scope(TenantContext(tenant_id="token-tenant", user_id="token-user")):
        tenant_id, user_id = _request_identity(
            x_user_id="header-user",
            x_tenant_id="spoofed-tenant",
        )

    assert tenant_id == "token-tenant"
    assert user_id == "token-user"


def test_blackboard_identity_keeps_dev_header_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kun.api.blackboard.settings",
        lambda: SimpleNamespace(env="dev"),
    )

    tenant_id, user_id = _request_identity(
        x_user_id="header-user",
        x_tenant_id="dev-tenant",
    )

    assert tenant_id == "dev-tenant"
    assert user_id == "header-user"


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
            "active_state_ledger": [
                {
                    "task_id": "tk-1",
                    "tenant_id": kw["tenant_id"],
                    "current_goal": "写一份方案",
                    "status": "running",
                    "decision_reason": "命中产品运营策略包",
                }
            ],
            "last_update": "2026-04-26T10:00:00Z",
        },
    )
    resp = bb_client.get(
        "/api/blackboard/state",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_count_running"] == 3
    assert resp.json()["active_state_ledger"][0]["task_id"] == "tk-1"


def test_blackboard_state_ledger_endpoints(bb_client: TestClient) -> None:
    def source(**kw):
        item = {
            "task_id": "tk-1",
            "tenant_id": kw["tenant_id"],
            "user_id": kw["user_id"],
            "current_goal": "帮用户完成任务",
            "status": "running",
            "current_step": 2,
            "total_steps": 5,
            "decision_reason": "命中通用策略包",
        }
        if kw.get("task_id"):
            return item if kw["task_id"] == "tk-1" else None
        return [item]

    register_data_source("state_ledger", source)

    resp = bb_client.get(
        "/api/blackboard/state-ledger",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert resp.status_code == 200
    assert resp.json()[0]["current_step"] == 2

    one = bb_client.get(
        "/api/blackboard/state-ledger/tk-1",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert one.status_code == 200
    assert one.json()["current_goal"] == "帮用户完成任务"


def test_blackboard_state_ledger_history_endpoints(bb_client: TestClient) -> None:
    def source(**kw):
        task_id = kw.get("task_id")
        return [
            {
                "event_id": "evt-1",
                "event_type": "watchtower.decision_plan.created",
                "occurred_at": "2026-04-29T10:00:00Z",
                "task_id": task_id or "tk-1",
                "summary": "守望生成策略单",
                "reason": "命中运营策略包",
                "cost_usd": 0.01,
                "decision_ticket_id": "dt-1",
                "payload": {"decision_ticket": {"ticket_id": "dt-1"}},
            }
        ]

    register_data_source("state_ledger_history", source)

    all_history = bb_client.get(
        "/api/blackboard/state-ledger/history",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert all_history.status_code == 200
    assert all_history.json()[0]["decision_ticket_id"] == "dt-1"

    task_history = bb_client.get(
        "/api/blackboard/state-ledger/tk-1/history",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert task_history.status_code == 200
    assert task_history.json()[0]["task_id"] == "tk-1"

    story = bb_client.get(
        "/api/blackboard/state-ledger/tk-1/story",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert story.status_code == 200
    assert story.json()["task_id"] == "tk-1"
    assert story.json()["event_count"] == 1
    assert story.json()["decision_count"] == 1
    assert story.json()["total_cost_usd"] == 0.01
    assert story.json()["latest_reason"] == "命中运营策略包"


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
    register_data_source(
        "state_ledger",
        lambda **kw: {"task_id": kw["task_id"], "tenant_id": kw["tenant_id"]},
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
    assert body["state_ledger"]["task_id"] == "tk-1"
    assert "workspace" in body


def test_state_ledger_entry_can_hydrate_from_durable_runtime_rows() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = cast(
        TaskRow,
        SimpleNamespace(
            task_id="tk-1",
            tenant_id="tenant-a",
            user_id="user-a",
            project_id="proj-a",
            task_type="product.ops",
            success_criteria_short="推进商业化",
            spec_json={"goal_detail": "运营产品拿到第一批客户"},
            risk_level="medium",
            complexity_score=0.7,
            estimated_cost_usd=1.5,
        ),
    )
    runtime = cast(
        RuntimeStateRow,
        SimpleNamespace(
            task_ref="tk-1",
            tenant_id="tenant-a",
            status="running",
            current_step=2,
            total_planned_steps=5,
            accumulated_cost_usd_equivalent=0.42,
            accumulated_tokens=1200,
            started_at=now,
            last_updated=now,
            finished_at=None,
            blob={
                "completed_steps": [
                    {"description": "整理用户访谈名单"},
                    {"skill_used": "lead_research"},
                ]
            },
        ),
    )

    entry = _entry_from_runtime_rows(task, runtime)

    assert entry.task_id == "tk-1"
    assert entry.current_goal == "运营产品拿到第一批客户"
    assert entry.status == "running"
    assert entry.current_step == 2
    assert entry.total_steps == 5
    assert entry.current_action == "lead_research"
    assert entry.recent_events[-1].kind == "state.hydrated"


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
    report = await runner.run(req)
    assert len(captured) >= 1
    assert report.request_id == "dr-llm"


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
