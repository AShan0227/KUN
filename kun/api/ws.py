"""WebSocket dialog protocol (ADR-010 KUN 风格).

Message format:
    Client → server:
        {"type": "user_message", "content": "..."}
        {"type": "interrupt"}
        {"type": "correction", "content": "..."}      # 纠偏即说

    Server → client (main channel):
        {"type": "thinking", ...}
        {"type": "action_plan", ...}
        {"type": "action", ...}
        {"type": "answer", "content": "..."}
        {"type": "ask_user", ...}
        {"type": "correction_ack", ...}
        {"type": "error", ...}

    Server → client (side channel):
        {"type": "cost_tick", ...}
        {"type": "insight", ...}
        {"type": "surprise", ...}
        {"type": "alert", ...}
        {"type": "guard_intervention", ...}
        {"type": "idle_batch_report", ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from kun.api.runtime import get_orchestrator
from kun.core.logging import get_logger
from kun.core.tenancy import TenantContext, tenant_scope
from kun.engineering.orchestrator import OrchestratorEvent

log = get_logger("kun.api.ws")

ws_router = APIRouter()

# A few conversational cues that auto-flag user correction (ADR-010 "纠偏即说")
_CORRECTION_CUES_CN = (
    "不是这样",
    "不对",
    "停",
    "换个思路",
    "不是这样做",
    "重新来",
)
_CORRECTION_CUES_EN = (
    "stop",
    "no that's",
    "no, that's",
    "wait,",
    "wrong",
    "try again",
    "different approach",
)


def _is_correction(text: str) -> bool:
    lower = text.lower()
    return any(c in text for c in _CORRECTION_CUES_CN) or any(
        c in lower for c in _CORRECTION_CUES_EN
    )


@ws_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    tenant_id = ws.query_params.get("tenant_id", "u-sylvan")
    user_id = ws.query_params.get("user_id")
    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id)

    log.info("ws.connected", tenant_id=tenant_id, user_id=user_id)

    try:
        with tenant_scope(ctx):
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue

                mtype = msg.get("type", "user_message")
                content = msg.get("content", "")

                if mtype == "user_message":
                    if _is_correction(content):
                        await ws.send_json({"type": "correction_ack", "content": content})
                        # fall through and run it like a user_message anyway —
                        # the model will be told upstream that this is a correction.
                    await _run_task_stream(ws, content)
                elif mtype == "correction":
                    await ws.send_json({"type": "correction_ack", "content": content})
                    await _run_task_stream(ws, content)
                elif mtype == "interrupt":
                    # Current skeleton: no pre-emptive interrupt yet.
                    await ws.send_json({"type": "correction_ack", "content": "interrupted"})
                else:
                    await ws.send_json(
                        {"type": "error", "message": f"unknown message type: {mtype}"}
                    )
    except WebSocketDisconnect:
        log.info("ws.disconnected", tenant_id=tenant_id)
    except Exception as e:
        log.exception("ws.error", error=str(e))
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": str(e)})


async def _run_task_stream(ws: WebSocket, user_message: str) -> None:
    """Run the orchestrator and forward events to the socket."""
    try:
        async for ev in get_orchestrator(ws.scope["app"]).stream(user_message):
            await ws.send_json(_event_to_wire(ev))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})


def _event_to_wire(ev: OrchestratorEvent) -> dict[str, Any]:
    """Translate OrchestratorEvent → wire format."""
    return {"type": ev.kind, **ev.data}
