from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kun.core.orm import PendingActionRow
from kun.world.gateway import (
    EmailDraftHandler,
    EmailSendHandler,
    LocalFileWriteHandler,
    WorldGateway,
)
from kun.world.handler_health import build_world_handler_health


def _row(
    action_id: str,
    action_type: str,
    status: str,
    *,
    gateway: dict[str, object] | None = None,
    risk_level: str = "medium",
) -> PendingActionRow:
    return PendingActionRow(
        action_id=action_id,
        tenant_id="tenant-1",
        task_ref="tk-1",
        action_type=action_type,
        target_ref="target",
        status=status,
        risk_level=risk_level,
        payload={"executor": {"gateway": gateway or {}}},
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
    card = cards[0]

    assert card.action_type == "message.send"
    assert card.status == "unregistered"
    assert card.success_rate == 0.0
    assert card.missing_handler_count == 1


def test_handler_health_flags_real_external_handler_as_limited(tmp_path: Path) -> None:
    handler = EmailSendHandler(
        output_root=tmp_path,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_from="kun@example.com",
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
    card = cards[0]

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
