from __future__ import annotations

from kun.engineering.nuo_system_health import _findings
from kun.world.handler_health import WorldHandlerHealthCard


def test_system_health_surfaces_limited_real_world_handler_as_finding() -> None:
    card = WorldHandlerHealthCard(
        action_type="email.send",
        handler_id="email.send.smtp.v1",
        status="limited",
        mode="execute",
        external_dispatched=True,
        registered=True,
        configured=False,
        requires_human_approval=True,
        has_compensation=True,
        static_risk="high",
        dynamic_risk="low",
        total_seen=1,
        approved_count=1,
        rejected_count=0,
        executed_count=1,
        failed_count=0,
        missing_handler_count=0,
        policy_blocked_count=0,
        success_rate=1.0,
        failure_rate=0.0,
        approval_reject_rate=0.0,
        compensation_strategy="无法自动撤回已送达邮件；只能发送更正邮件或人工跟进。",
        recommendation="保留人工确认；不要自动外发；先补齐补偿和失败复盘。",
        issues=["真实外发风险高：会影响外部系统，必须人工确认和审计", "缺少全局或租户级环境变量"],
    )

    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=False,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[card],
    )

    assert len(findings) == 1
    assert findings[0].finding_id == "world:email.send"
    assert findings[0].severity == "warn"
    assert "真实外发风险高" in findings[0].detail


def test_system_health_surfaces_disabled_mission_resume_worker() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=2,
        mission_resume_worker_enabled=False,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
    )

    assert any(item.finding_id == "mission_resume_worker_disabled" for item in findings)
