from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kun.core.orm import PendingActionRow, WorldActionExecutionRow
from kun.ops.secret_store import SECRET_STORE_FILE_ENV
from kun.world.gateway import (
    EmailDraftHandler,
    EmailSendHandler,
    LocalFileWriteHandler,
    WorldGateway,
)
from kun.world.handler_control import WorldHandlerControl
from kun.world.handler_health import build_world_handler_health


def _row(
    action_id: str,
    action_type: str,
    status: str,
    *,
    gateway: dict[str, object] | None = None,
    risk_level: str = "medium",
    executor_status: str | None = None,
) -> PendingActionRow:
    executor: dict[str, object] = {"gateway": gateway or {}}
    if executor_status is not None:
        executor["status"] = executor_status
    return PendingActionRow(
        action_id=action_id,
        tenant_id="tenant-1",
        task_ref="tk-1",
        action_type=action_type,
        target_ref="target",
        status=status,
        risk_level=risk_level,
        payload={"executor": executor},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _execution(
    action_id: str,
    action_type: str,
    status: str,
    *,
    gateway_mode: str = "handler_executed",
    capability_status: str = "supported_execute",
    external_dispatched: bool = False,
    requires_handler: bool = False,
    handler_id: str | None = None,
) -> WorldActionExecutionRow:
    return WorldActionExecutionRow(
        tenant_id="tenant-1",
        action_id=action_id,
        task_ref="tk-1",
        action_type=action_type,
        target_ref="target",
        idempotency_key=action_id,
        status=status,
        attempt_count=1,
        handler_id=handler_id,
        gateway_mode=gateway_mode,
        capability_status=capability_status,
        external_dispatched=external_dispatched,
        requires_handler=requires_handler,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_handler_health_does_not_count_missing_handler_as_success(tmp_path: Path) -> None:
    rows = [
        _row(
            "act-1",
            "message.send",
            "executed",
            gateway={
                "gateway_mode": "approval_gate",
                "requires_handler": True,
                "capability_status": "missing_handler",
            },
        )
    ]

    cards = build_world_handler_health(descriptors=[], rows=rows)
    card = next(card for card in cards if card.action_type == "message.send")

    assert card.action_type == "message.send"
    assert card.status == "unregistered"
    assert card.success_rate == 0.0
    assert card.missing_handler_count == 1


def test_handler_health_consumes_persistent_quarantine(tmp_path: Path) -> None:
    descriptors = WorldGateway(
        artifact_root=tmp_path,
        handlers=[EmailDraftHandler(tmp_path / "drafts")],
    ).handler_descriptors()

    cards = build_world_handler_health(
        descriptors=descriptors,
        rows=[],
        tenant_id="tenant-1",
        controls={
            "email.draft": WorldHandlerControl(
                tenant_id="tenant-1",
                action_type="email.draft",
                status="quarantined",
                reason="recent failures",
            )
        },
    )
    card = next(card for card in cards if card.action_type == "email.draft")

    assert card.status == "blocked"
    assert card.control_status == "quarantined"
    assert "recent failures" in card.control_reason
    assert any("持久化隔离" in issue for issue in card.issues)
    assert "restore" in card.recommendation


def test_handler_health_flags_real_external_handler_as_limited(tmp_path: Path) -> None:
    handler = EmailSendHandler(
        output_root=tmp_path,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_from="kun@example.com",
        allowed_recipient_domains={"example.com"},
        sender=lambda _message: {"provider_message_id": "ok"},
    )
    rows = [
        _row(
            "act-1",
            "email.send",
            "executed",
            gateway={
                "gateway_mode": "handler_executed",
                "requires_handler": False,
                "capability_status": "supported_execute",
            },
            risk_level="high",
        )
    ]

    descriptors = WorldGateway(artifact_root=tmp_path, handlers=[handler]).handler_descriptors()
    cards = build_world_handler_health(descriptors=descriptors, rows=rows)
    card = next(card for card in cards if card.action_type == "email.send")

    assert card.action_type == "email.send"
    assert card.external_dispatched is True
    assert card.success_rate == 1.0
    assert card.status == "limited"
    assert "人工确认" in card.recommendation


def test_handler_health_counts_policy_blocked_as_risk(tmp_path: Path) -> None:
    descriptors = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            LocalFileWriteHandler(tmp_path / "files"),
            EmailDraftHandler(tmp_path / "drafts"),
        ],
    ).handler_descriptors()
    rows = [
        _row(
            "act-1",
            "local_file.write",
            "executed",
            gateway={
                "gateway_mode": "policy_blocked",
                "requires_handler": False,
                "capability_status": "supported_execute",
            },
        ),
        _row("act-2", "email.draft", "rejected"),
    ]

    cards = {
        card.action_type: card
        for card in build_world_handler_health(
            descriptors=descriptors,
            rows=rows,
        )
    }

    assert cards["local_file.write"].policy_blocked_count == 1
    assert cards["local_file.write"].failure_rate == 1.0
    assert cards["email.draft"].rejected_count == 1


def test_handler_health_surfaces_expected_real_handler_config_gaps(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KUN_WORLD_EMAIL_SEND_ENABLED", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_HOST", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_FROM", raising=False)

    cards = {card.action_type: card for card in build_world_handler_health(descriptors=[], rows=[])}

    email = cards["email.send"]
    assert email.status == "unregistered"
    assert any("KUN_WORLD_EMAIL_SEND_ENABLED" in issue for issue in email.issues)
    assert any("KUN_WORLD_SMTP_HOST" in issue for issue in email.issues)
    assert "环境变量" in email.recommendation
    assert "KUN_WORLD_EMAIL_SEND_ENABLED" in email.missing_env_vars
    assert "KUN_WORLD_SMTP_HOST" in email.missing_env_vars
    assert any("补齐配置" in step for step in email.setup_steps)


def test_handler_health_accepts_tenant_scoped_expected_handler_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KUN_WORLD_EMAIL_SEND_ENABLED", "true")
    monkeypatch.delenv("KUN_WORLD_SMTP_HOST", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_FROM", raising=False)
    monkeypatch.setenv("KUN_TENANT_TENANT_1_WORLD_SMTP_HOST", "smtp.tenant.example.com")
    monkeypatch.setenv("KUN_TENANT_TENANT_1_WORLD_SMTP_FROM", "tenant@example.com")
    monkeypatch.setenv("KUN_TENANT_TENANT_1_WORLD_EMAIL_ALLOWED_DOMAINS", "example.com")

    cards = {
        card.action_type: card
        for card in build_world_handler_health(
            descriptors=[],
            rows=[_row("act-tenant", "email.send", "approved")],
        )
    }

    email = cards["email.send"]
    assert email.status == "unregistered"
    assert not any("KUN_WORLD_EMAIL_SEND_ENABLED" in issue for issue in email.issues)
    assert not any("SMTP_HOST" in issue for issue in email.issues)
    assert email.missing_env_vars == []


def test_handler_health_reads_expected_handler_config_from_secret_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = tmp_path / "secrets.json"
    store.write_text(
        json.dumps(
            {
                "global": {"KUN_WORLD_EMAIL_SEND_ENABLED": "true"},
                "tenants": {
                    "tenant-1": {
                        "KUN_WORLD_SMTP_HOST": "smtp.secret.example.com",
                        "KUN_WORLD_SMTP_FROM": "kun@secret.example.com",
                        "KUN_WORLD_EMAIL_ALLOWED_DOMAINS": "example.com",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SECRET_STORE_FILE_ENV, str(store))
    monkeypatch.delenv("KUN_WORLD_EMAIL_SEND_ENABLED", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_HOST", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_FROM", raising=False)

    cards = {
        card.action_type: card
        for card in build_world_handler_health(
            descriptors=[],
            rows=[_row("act-secret", "email.send", "approved")],
        )
    }

    email = cards["email.send"]
    assert email.status == "unregistered"
    assert not any("KUN_WORLD_EMAIL_SEND_ENABLED" in issue for issue in email.issues)
    assert not any("SMTP_HOST" in issue for issue in email.issues)
    assert email.missing_env_vars == []


def test_handler_health_flags_half_enabled_real_external_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KUN_WORLD_EMAIL_SEND_ENABLED", raising=False)
    monkeypatch.setenv("KUN_WORLD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("KUN_WORLD_SMTP_FROM", "kun@example.com")

    cards = {card.action_type: card for card in build_world_handler_health(descriptors=[], rows=[])}

    email = cards["email.send"]
    assert email.configured is False
    assert any("真实外发半启用" in issue for issue in email.issues)
    assert any("KUN_WORLD_EMAIL_SEND_ENABLED" in issue for issue in email.issues)
    assert email.missing_env_vars == [
        "KUN_WORLD_EMAIL_SEND_ENABLED",
        "KUN_WORLD_EMAIL_ALLOWED_DOMAINS",
    ]


def test_handler_health_flags_high_failure_rate(tmp_path: Path) -> None:
    descriptors = WorldGateway(
        artifact_root=tmp_path,
        handlers=[EmailDraftHandler(tmp_path / "drafts")],
    ).handler_descriptors()
    rows = [
        _row(
            "act-ok-1",
            "email.draft",
            "executed",
            gateway={
                "gateway_mode": "handler_executed",
                "requires_handler": False,
                "capability_status": "supported_draft",
            },
        ),
        _row(
            "act-ok-2",
            "email.draft",
            "executed",
            gateway={
                "gateway_mode": "handler_executed",
                "requires_handler": False,
                "capability_status": "supported_draft",
            },
        ),
        _row(
            "act-ok-3",
            "email.draft",
            "executed",
            gateway={
                "gateway_mode": "handler_executed",
                "requires_handler": False,
                "capability_status": "supported_draft",
            },
        ),
        _row("act-fail", "email.draft", "approved", executor_status="failed"),
    ]

    cards = {
        card.action_type: card
        for card in build_world_handler_health(descriptors=descriptors, rows=rows)
    }

    email = cards["email.draft"]
    assert email.status == "blocked"
    assert email.failure_rate == 0.25
    assert any("失败率高" in issue for issue in email.issues)


def test_handler_health_prefers_durable_execution_ledger(tmp_path: Path) -> None:
    descriptors = WorldGateway(
        artifact_root=tmp_path,
        handlers=[EmailDraftHandler(tmp_path / "drafts")],
    ).handler_descriptors()
    rows = [
        # The approval row still says approved, but the durable execution ledger
        # is the stronger source of truth for handler health.
        _row("act-1", "email.draft", "approved"),
    ]
    executions = [
        _execution(
            "act-1",
            "email.draft",
            "executed",
            capability_status="supported_draft",
            handler_id="email.draft.v1",
        )
    ]

    cards = {
        card.action_type: card
        for card in build_world_handler_health(
            descriptors=descriptors,
            rows=rows,
            executions=executions,
        )
    }

    assert cards["email.draft"].executed_count == 1
    assert cards["email.draft"].success_rate == 1.0
    assert cards["email.draft"].failure_rate == 0.0


def test_handler_health_counts_durable_blocked_execution_as_failure(tmp_path: Path) -> None:
    descriptors = WorldGateway(
        artifact_root=tmp_path,
        handlers=[EmailDraftHandler(tmp_path / "drafts")],
    ).handler_descriptors()
    executions = [
        _execution(
            "act-1",
            "email.draft",
            "blocked",
            gateway_mode="policy_blocked",
            capability_status="preview_failed",
            requires_handler=True,
        )
    ]

    cards = {
        card.action_type: card
        for card in build_world_handler_health(
            descriptors=descriptors,
            rows=[],
            executions=executions,
        )
    }

    assert cards["email.draft"].failure_rate == 1.0
    assert cards["email.draft"].policy_blocked_count == 1
    assert cards["email.draft"].missing_handler_count == 1
