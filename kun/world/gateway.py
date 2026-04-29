"""V3 World Gateway.

World Gateway is the only module allowed to prepare real-world side effects.
The first production-safe slice is deliberately conservative: it creates an
audit packet and releases the approval gate, but it does not send emails, call
paid APIs, publish content, or move money until explicit delivery handlers are
registered in a later slice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.interface.hermes import DefaultHermesAdapter, HermesAdapter


class WorldAction(BaseModel):
    """A tenant-scoped side-effect request."""

    action_id: str
    task_ref: str
    action_type: str
    target_ref: str
    risk_level: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorldGatewayResult(BaseModel):
    """Gateway audit result."""

    action_id: str
    gateway_mode: str = "approval_gate"
    external_dispatched: bool = False
    requires_handler: bool = True
    rendered_payload: str = ""
    audit: dict[str, Any] = Field(default_factory=dict)
    message: str = (
        "World Gateway recorded and authorized this action, but no external "
        "delivery handler is attached yet."
    )


class WorldGateway:
    """Prepare and audit side-effect actions."""

    def __init__(self, *, hermes_adapter: HermesAdapter | None = None) -> None:
        self.hermes_adapter = hermes_adapter or DefaultHermesAdapter()

    async def execute_approved(self, action: WorldAction) -> WorldGatewayResult:
        target = self._target_for(action.action_type)
        packet = await self.hermes_adapter.translate_external(
            target=target,
            payload={
                "action_id": action.action_id,
                "task_ref": action.task_ref,
                "action_type": action.action_type,
                "target_ref": action.target_ref,
                "payload": action.payload,
            },
            context={
                "risk_level": action.risk_level,
                "gateway_mode": "approval_gate",
                "method": "side_effect.prepare",
            },
        )
        now = datetime.now(UTC).isoformat()
        return WorldGatewayResult(
            action_id=action.action_id,
            rendered_payload=packet.rendered,
            audit={
                "prepared_at": now,
                "target": target,
                "risk_level": action.risk_level,
                "action_type": action.action_type,
                "external_dispatched": False,
                "reason": "no delivery handler registered in V3-5 first slice",
            },
        )

    def _target_for(self, action_type: str) -> Literal["api", "external_agent", "human"]:
        if action_type.startswith(("message.", "content.", "payment.", "deployment.")):
            return "api"
        if action_type.startswith("external_agent."):
            return "external_agent"
        return "human"


_gateway: WorldGateway | None = None


def get_world_gateway() -> WorldGateway:
    global _gateway
    if _gateway is None:
        _gateway = WorldGateway()
    return _gateway


def set_world_gateway(gateway: WorldGateway) -> None:
    global _gateway
    _gateway = gateway


__all__ = [
    "WorldAction",
    "WorldGateway",
    "WorldGatewayResult",
    "get_world_gateway",
    "set_world_gateway",
]
