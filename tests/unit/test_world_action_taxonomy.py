"""WorldGateway action taxonomy alignment tests."""

from __future__ import annotations

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.concurrency import PendingActionSpec, pending_actions_for
from kun.world.action_taxonomy import normalize_world_action_type


def _task(
    *,
    task_type: str,
    text: str,
    external_resources: list[str] | None = None,
) -> TaskRef:
    owner = Owner(tenant_id="u-sylvan", project_id="proj-main")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint(text, owner),
        task_type=task_type,
        risk_level="low",
        owner=owner,
        success_criteria_short=text,
    )
    spec = TaskSpec(
        goal_detail=text,
        external_resources=external_resources or [],
    )
    return TaskRef(meta=meta, spec=spec)


@pytest.mark.unit
def test_message_send_defaults_to_email_draft_with_audit_fields() -> None:
    action = PendingActionSpec(
        action_type="message.send",
        target_ref="customer@example.com",
        payload={"subject": "Hello", "body": "Draft this first."},
    )

    assert action.action_type == "email.draft"
    assert action.payload["source_action_type"] == "message.send"
    assert action.payload["taxonomy_reason"] == "message_or_email_defaults_to_draft"
    assert action.payload["requires_real_dispatch_confirmation"] is True


@pytest.mark.unit
def test_content_publish_and_webhook_default_to_webhook_post_dry_run() -> None:
    actions = pending_actions_for(
        _task(
            task_type="content.publish",
            text="Prepare webhook payload",
            external_resources=["https://example.com/hook"],
        )
    )

    assert [action.action_type for action in actions] == ["webhook.post_dry_run"]
    assert actions[0].payload["source_action_type"] == "content.publish"
    assert actions[0].payload["taxonomy_reason"] == ("webhook_or_publish_defaults_to_post_dry_run")


@pytest.mark.unit
def test_browser_action_defaults_to_browser_plan() -> None:
    action = PendingActionSpec(
        action_type="browser.execute",
        target_ref="https://example.com",
        payload={"url": "https://example.com", "steps": [{"kind": "click"}]},
    )

    assert action.action_type == "browser.plan"
    assert action.payload["source_action_type"] == "browser.execute"
    assert action.payload["taxonomy_reason"] == "browser_action_defaults_to_plan"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_action_type", "expected"),
    [
        ("email.send", "email.send"),
        ("browser.execute", "browser.execute"),
        ("enterprise_api.post", "enterprise_api.post"),
    ],
)
def test_explicit_confirmation_allows_real_dispatch_handlers(
    source_action_type: str,
    expected: str,
) -> None:
    result = normalize_world_action_type(
        source_action_type,
        {"external_dispatch_confirmed": True},
    )

    assert result.action_type == expected
    assert result.requires_real_dispatch_confirmation is True


@pytest.mark.unit
def test_enterprise_api_without_confirmation_stays_dry_run() -> None:
    action = PendingActionSpec(
        action_type="enterprise_api.post",
        target_ref="https://api.example.com/events",
        payload={"json": {"ok": True}},
    )

    assert action.action_type == "webhook.post_dry_run"
    assert action.payload["source_action_type"] == "enterprise_api.post"
    assert action.payload["requires_real_dispatch_confirmation"] is True
