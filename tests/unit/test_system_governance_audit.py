from __future__ import annotations

from kun.engineering.delivery_status import DeliveryCapability
from kun.engineering.system_governance_audit import run_system_governance_audit
from kun.world.handler_health import WorldHandlerHealthCard


def test_system_governance_audit_flags_missing_decision_ticket_for_route_governance() -> None:
    report = run_system_governance_audit(
        tenant_id="tenant-a",
        decision_event_samples=[
            {
                "event_type": "llm.model_select.consulted",
                "task_ref": "task-route-1",
                "payload": {
                    "task_type": "coding.review",
                    "selected_model": "gpt-5.5",
                    "selected_score": 0.91,
                },
            }
        ],
    )

    issue = next(
        item
        for item in report.issues
        if item.issue_id == "decision_missing_ticket:llm.model_select.consulted:task-route-1"
    )
    assert issue.severity == "warn"
    assert issue.category == "decision_coverage"
    assert issue.task_id == "task-route-1"
    assert issue.evidence["event_type"] == "llm.model_select.consulted"


def test_system_governance_audit_accepts_route_governance_with_decision_ticket() -> None:
    report = run_system_governance_audit(
        tenant_id="tenant-a",
        decision_event_samples=[
            {
                "event_type": "llm.model_select.consulted",
                "task_ref": "task-route-2",
                "payload": {
                    "decision_ticket": {
                        "ticket_id": "dt-route-2",
                        "task_id": "task-route-2",
                        "decision_point": "llm_model_selected",
                        "source_module": "watchtower.llm_route_governance",
                        "selected_action": "gpt-5.5",
                        "status": "selected",
                    }
                },
            }
        ],
    )

    assert not any(item.issue_id.startswith("decision_missing_ticket:") for item in report.issues)


def test_system_governance_audit_flags_decision_mode_conflict() -> None:
    report = run_system_governance_audit(
        tenant_id="tenant-a",
        decision_event_samples=[
            {
                "event_type": "task.execution_mode.selected",
                "task_ref": "task-1",
                "payload": {
                    "decision_ticket": {
                        "ticket_id": "dt-1",
                        "task_id": "task-1",
                        "decision_point": "execution_mode_selected",
                        "source_module": "execution_mode_classifier",
                        "selected_action": "FAST",
                        "status": "selected",
                    }
                },
            },
            {
                "event_type": "task.strategy.selected",
                "task_ref": "task-1",
                "payload": {
                    "decision_ticket": {
                        "ticket_id": "dt-2",
                        "task_id": "task-1",
                        "decision_point": "strategy_selected",
                        "source_module": "watchtower.decision_plane",
                        "selected_action": "pack-sales:MAX",
                        "status": "applied",
                        "metadata": {"execution_mode": "MAX"},
                    }
                },
            },
        ],
    )

    issue = next(item for item in report.issues if item.issue_id == "decision_mode_conflict:task-1")
    assert issue.severity == "warn"
    assert issue.category == "decision_conflict"
    assert issue.task_id == "task-1"
    assert set(issue.evidence["mode_by_source"].values()) == {"FAST", "MAX"}


def test_system_governance_audit_flags_blocked_then_delivered() -> None:
    report = run_system_governance_audit(
        tenant_id="tenant-a",
        decision_event_samples=[
            {
                "event_type": "task.preflight.blocked",
                "task_ref": "task-2",
                "payload": {
                    "decision_ticket": {
                        "ticket_id": "dt-3",
                        "task_id": "task-2",
                        "decision_point": "preflight_guard",
                        "source_module": "engineering.preflight",
                        "selected_action": "block",
                        "status": "blocked",
                    }
                },
            },
            {
                "event_type": "task.delivery.reviewed",
                "task_ref": "task-2",
                "payload": {
                    "decision_ticket": {
                        "ticket_id": "dt-4",
                        "task_id": "task-2",
                        "decision_point": "delivery_review",
                        "source_module": "engineering.pre_deliver_gate",
                        "selected_action": "done",
                        "status": "allowed",
                    }
                },
            },
        ],
    )

    issue = next(
        item for item in report.issues if item.issue_id == "decision_blocked_then_delivered:task-2"
    )
    assert issue.severity == "error"
    assert "blocking" in issue.detail


def test_system_governance_audit_flags_world_and_scheduler_risks() -> None:
    handler = WorldHandlerHealthCard(
        action_type="email.send",
        status="limited",
        external_dispatched=True,
        registered=True,
        configured=False,
        has_compensation=False,
        secret_config_status="half_enabled",
        missing_env_vars=["KUN_WORLD_SMTP_HOST"],
        risk_flags=["missing_compensation", "half_enabled_secret_config"],
        recommendation="补齐补偿和 SMTP 配置。",
    )

    report = run_system_governance_audit(
        tenant_id="tenant-a",
        world_handlers=[handler],
        scheduler_summary={"missing_required_lanes": 1, "lanes_over_pressure_threshold": 1},
        scheduler_limits={"fast": 4, "mission": 2},
    )

    by_id = {item.issue_id: item for item in report.issues}
    assert by_id["world_missing_compensation:email.send"].severity == "error"
    assert by_id["world_secret_config:email.send"].severity == "error"
    assert by_id["scheduler_missing_required_lanes"].severity == "error"
    assert by_id["scheduler_missing_world_lane_for_handlers"].category == "scheduler"


def test_system_governance_audit_flags_delivery_honesty_risk() -> None:
    report = run_system_governance_audit(
        tenant_id="tenant-a",
        delivery_items=[
            DeliveryCapability(
                capability_id="world_gateway",
                label="外部世界",
                status="partial",
                summary="已有审计网关，还没全部真实执行。",
                done=["有审计"],
                missing=["缺 handler"],
                evidence_refs=["kun/world/gateway.py"],
            )
        ],
        delivery_validation_issues=["demo: ready capability still has missing items"],
    )

    by_id = {item.issue_id: item for item in report.issues}
    assert any(item.startswith("delivery_validation:") for item in by_id)
    assert by_id["delivery_public_incomplete_capabilities"].severity == "info"
    assert report.summary["category:delivery_status"] == 2
