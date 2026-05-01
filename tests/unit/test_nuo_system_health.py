from __future__ import annotations

from kun.engineering.nuo_system_health import _findings, _governance_recommendations
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


def test_system_health_surfaces_coordination_issues() -> None:
    from kun.engineering.system_coordination import CoordinationIssue

    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=False,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        coordination_issues=[
            CoordinationIssue(
                issue_id="paused_without_gate:task-1",
                severity="warn",
                title="任务暂停了，但没有可见的待确认动作",
                detail="task-1 paused without gate",
                suggested_action="检查 RuntimeState。",
                task_id="task-1",
            )
        ],
    )

    assert any(item.finding_id == "coordination:paused_without_gate:task-1" for item in findings)


def test_system_health_surfaces_state_ledger_drift() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        state_ledger_audit_summary={
            "checked": 3,
            "missing_history": 0,
            "status_drift": 1,
            "cost_drift": 1,
            "history_gap": 0,
            "drift": 1,
        },
    )

    assert any(item.finding_id == "state_ledger_drift" for item in findings)
    assert (
        next(item for item in findings if item.finding_id == "state_ledger_drift").severity
        == "error"
    )


def test_system_health_surfaces_state_ledger_missing_history() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        state_ledger_audit_summary={
            "checked": 2,
            "missing_history": 1,
            "status_drift": 0,
            "cost_drift": 0,
            "history_gap": 0,
            "drift": 0,
        },
    )

    assert any(item.finding_id == "state_ledger_missing_history" for item in findings)


def test_system_health_surfaces_context_maintenance_candidates() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        context_maintenance_summary={
            "total_seen": 12,
            "compressed": 2,
            "soft_forgotten": 3,
            "hard_deleted": 1,
            "duplicate_candidates": 4,
            "kept": 2,
        },
    )

    by_id = {item.finding_id: item for item in findings}
    assert by_id["context_hard_delete_candidates"].severity == "warn"
    assert by_id["context_slimming_candidates"].severity == "info"
    assert "可压缩 2" in by_id["context_slimming_candidates"].detail


def test_system_health_surfaces_compiler_recompile_candidates() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        context_maintenance_summary={
            "compiler_review": 0,
            "compiler_recompile_recommended": 2,
        },
    )

    finding = next(item for item in findings if item.finding_id == "compiler_recompile_candidates")
    assert finding.subsystem == "compiler"
    assert finding.severity == "warn"


def test_system_health_surfaces_context_maintenance_error() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        context_maintenance_error="redis unavailable",
    )

    finding = next(item for item in findings if item.finding_id == "context_maintenance_error")
    assert finding.severity == "warn"
    assert "redis unavailable" in finding.detail


def test_system_health_surfaces_skill_governance_findings() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        skill_health_summary={
            "manifest_without_executor": 2,
            "weak_capability_cards": 1,
            "unused_manifest_skills": 3,
        },
    )

    by_id = {item.finding_id: item for item in findings}
    assert by_id["skill_manifest_without_executor"].severity == "warn"
    assert by_id["skill_weak_capability_cards"].subsystem == "skill"
    assert by_id["skill_unused_manifest"].severity == "info"


def test_system_health_surfaces_qi_strategy_draft_findings() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        qi_strategy_draft_summary={
            "drafts": 3,
            "production_action_true": 1,
            "needs_strong_review": 2,
            "review_needs_evidence": 1,
        },
    )

    by_id = {item.finding_id: item for item in findings}
    assert by_id["qi_strategy_draft_production_action"].severity == "critical"
    assert by_id["qi_strategy_drafts_need_review"].severity == "warn"


def test_system_health_surfaces_scheduler_and_production_findings() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        multi_lane_scheduler_summary={
            "missing_required_lanes": 1,
            "lanes_over_pressure_threshold": 1,
        },
        production_risk_summary={
            "production_safety_issues": 2,
            "partial_or_not_ready_capabilities": 4,
        },
        production_risk_issues=[
            "KUN_DEFAULT_TENANT_ID must be blank in production",
            "S3/MinIO default credentials must be changed in production",
        ],
    )

    by_id = {item.finding_id: item for item in findings}
    assert by_id["scheduler_missing_required_lanes"].severity == "error"
    assert by_id["scheduler_lane_pressure"].subsystem == "scheduler"
    assert by_id["production_safety_issues"].severity == "critical"


def test_governance_recommendations_keep_high_risk_advice_manual() -> None:
    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        resumable_mission_task_count=0,
        mission_resume_worker_enabled=True,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[],
        world_handlers=[],
        context_maintenance_summary={"compressed": 2, "soft_forgotten": 1},
        production_risk_summary={"production_safety_issues": 1},
        production_risk_issues=["KUN_AUTH_SECRET missing"],
    )

    recommendations = _governance_recommendations(findings=findings)
    by_finding = {item.finding_id: item for item in recommendations}
    assert by_finding["context_slimming_candidates"].can_apply is True
    assert by_finding["context_slimming_candidates"].default_dry_run is True
    assert by_finding["production_safety_issues"].can_apply is False
    assert by_finding["production_safety_issues"].requires_human_approval is True
