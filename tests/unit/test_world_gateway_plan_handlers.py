"""Plan-only WorldGateway handlers for high-risk external actions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from kun.world.gateway import WorldAction, WorldGateway


def _action(action_type: str, payload: dict[str, Any] | None = None) -> WorldAction:
    return WorldAction(
        action_id=f"act-{action_type.replace('.', '-')}",
        tenant_id="tenant-1",
        task_ref="task-1",
        action_type=action_type,
        target_ref="target-1",
        risk_level="high",
        payload=payload or {"objective": "prepare operator-facing plan"},
    )


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.unit
def test_world_gateway_registers_high_risk_plan_handlers_by_default(tmp_path: Path) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    descriptors = {item.action_type: item for item in gateway.handler_descriptors()}

    for action_type in ("payment.plan", "content.publish_plan", "deployment.plan"):
        descriptor = descriptors[action_type]
        assert descriptor.mode == "plan"
        assert descriptor.external_dispatched is False
        assert descriptor.permissions_required
        assert descriptor.cannot_do
        assert descriptor.retry_policy
        assert descriptor.compensation_strategy

    assert "不能真实支付" in descriptors["payment.plan"].cannot_do
    assert "不能公开发布内容" in descriptors["content.publish_plan"].cannot_do
    assert "不能真实部署" in descriptors["deployment.plan"].cannot_do


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_type", "artifact_dir", "effect_flag"),
    [
        ("payment.plan", "payment_plans", "paid"),
        ("content.publish_plan", "publish_plans", "published"),
        ("deployment.plan", "deployment_plans", "deployed"),
    ],
)
async def test_plan_handler_preview_writes_only_plan_artifact(
    tmp_path: Path,
    action_type: str,
    artifact_dir: str,
    effect_flag: str,
) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.preview(
        _action(action_type, {"secret": "do-not-store", "objective": "review first"})
    )

    payload = json.loads(result.rendered_payload)

    assert result.gateway_mode == "handler_preview"
    assert result.capability_status == "supported_plan"
    assert result.external_dispatched is False
    assert result.requires_handler is False
    assert result.audit["external_dispatched"] is False
    assert result.audit["artifact_written"] is False
    assert result.audit[effect_flag] is False
    assert list((tmp_path / artifact_dir).glob("*.preview.json")) == []
    assert payload["mode"] == "plan"
    assert payload["external_dispatched"] is False
    assert payload[effect_flag] is False
    assert payload["payload"]["secret"] == "[redacted]"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_type", "artifact_dir", "effect_flag"),
    [
        ("payment.plan", "payment_plans", "paid"),
        ("content.publish_plan", "publish_plans", "published"),
        ("deployment.plan", "deployment_plans", "deployed"),
    ],
)
async def test_plan_handler_execute_approved_writes_draft_without_external_side_effect(
    tmp_path: Path,
    action_type: str,
    artifact_dir: str,
    effect_flag: str,
) -> None:
    gateway = WorldGateway(artifact_root=tmp_path)

    result = await gateway.execute_approved(_action(action_type, {"objective": "operator handoff"}))

    artifact_path = Path(result.audit["path"])
    artifact = _read_json(artifact_path)

    assert result.gateway_mode == "handler_drafted"
    assert result.capability_status == "supported_plan"
    assert result.external_dispatched is False
    assert result.requires_handler is False
    assert result.audit["handler_status"] == "drafted"
    assert result.audit["external_dispatched"] is False
    assert result.audit[effect_flag] is False
    assert artifact_path.parent == tmp_path / artifact_dir
    assert artifact_path.name.endswith(".plan.json")
    assert artifact["phase"] == "approved"
    assert artifact["external_dispatched"] is False
    assert artifact[effect_flag] is False
    assert artifact["retry_policy"]
    assert artifact["compensation_strategy"]
