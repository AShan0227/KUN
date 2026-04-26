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
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from kun.api.runtime import get_kill_switch, get_orchestrator
from kun.core.logging import get_logger
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    resolve_tenant_id,
    tenant_scope,
)
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
    try:
        tenant_id = resolve_tenant_id(ws.query_params.get("tenant_id"))
    except MissingTenantContextError:
        await ws.close(code=1008, reason="tenant_id required")
        return

    await ws.accept()
    user_id = ws.query_params.get("user_id")
    raw_audience = (ws.query_params.get("audience") or "developer").lower()
    audience = raw_audience if raw_audience in {"novice", "developer", "expert"} else "developer"
    default_output_kind = str(ws.query_params.get("output_kind") or "user")
    ctx = TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        audience=audience,  # type: ignore[arg-type]
    )
    send_lock = asyncio.Lock()
    current_task: asyncio.Task[None] | None = None
    current_task_id: str | None = None
    ks = get_kill_switch(ws.scope["app"])

    log.info("ws.connected", tenant_id=tenant_id, user_id=user_id)

    try:
        with tenant_scope(ctx):
            while True:
                current_task = _clear_finished_task(current_task)
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_json(ws, {"type": "error", "message": "invalid JSON"}, send_lock)
                    continue

                mtype = msg.get("type", "user_message")
                content = str(msg.get("content", ""))

                if mtype == "user_message":
                    output_kind = str(msg.get("output_kind") or default_output_kind)
                    if current_task is not None:
                        await _send_json(
                            ws,
                            {"type": "error", "message": "task already running"},
                            send_lock,
                        )
                        continue
                    if _is_correction(content):
                        await _send_json(
                            ws,
                            {"type": "correction_ack", "content": content},
                            send_lock,
                        )
                        # fall through and run it like a user_message anyway —
                        # the model will be told upstream that this is a correction.
                    current_task_id = f"ws-{uuid.uuid4().hex[:12]}"
                    ks.register_task(current_task_id)
                    current_task = asyncio.create_task(
                        _run_task_stream(ws, content, send_lock, output_kind=output_kind)
                    )
                elif mtype == "correction":
                    output_kind = str(msg.get("output_kind") or default_output_kind)
                    if current_task is not None:
                        if current_task_id:
                            ks.kill(current_task_id, reason="user_correction")
                        await _cancel_task(current_task)
                        if current_task_id:
                            ks.cleanup(current_task_id)
                    await _send_json(
                        ws,
                        {"type": "correction_ack", "content": content},
                        send_lock,
                    )
                    current_task_id = f"ws-{uuid.uuid4().hex[:12]}"
                    ks.register_task(current_task_id)
                    current_task = asyncio.create_task(
                        _run_task_stream(ws, content, send_lock, output_kind=output_kind)
                    )
                elif mtype == "interrupt":
                    if current_task is not None:
                        if current_task_id:
                            ks.kill(current_task_id, reason="user_interrupt")
                        await _cancel_task(current_task)
                        if current_task_id:
                            ks.cleanup(current_task_id)
                            current_task_id = None
                        current_task = None
                        content = "interrupted"
                    else:
                        content = "no running task"
                    await _send_json(
                        ws,
                        {"type": "correction_ack", "content": content},
                        send_lock,
                    )
                else:
                    await _send_json(
                        ws,
                        {"type": "error", "message": f"unknown message type: {mtype}"},
                        send_lock,
                    )
    except WebSocketDisconnect:
        log.info("ws.disconnected", tenant_id=tenant_id)
    except Exception as e:
        log.exception("ws.error", error=str(e))
        with contextlib.suppress(Exception):
            await _send_json(ws, {"type": "error", "message": str(e)}, send_lock)
    finally:
        if current_task is not None:
            if current_task_id:
                ks.kill(current_task_id, reason="ws_disconnect")
            await _cancel_task(current_task)
        if current_task_id:
            ks.cleanup(current_task_id)


async def _run_task_stream(
    ws: WebSocket,
    user_message: str,
    send_lock: asyncio.Lock,
    *,
    output_kind: str = "user",
) -> None:
    """Run the orchestrator and forward events to the socket."""
    try:
        async for ev in get_orchestrator(ws.scope["app"]).stream(
            user_message,
            output_kind=output_kind,
        ):
            await _send_json(ws, _event_to_wire(ev), send_lock)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await _send_json(ws, {"type": "error", "message": str(e)}, send_lock)


def _event_to_wire(ev: OrchestratorEvent) -> dict[str, Any]:
    """Translate OrchestratorEvent → wire format."""
    return {"type": ev.kind, **ev.data}


def _clear_finished_task(task: asyncio.Task[None] | None) -> asyncio.Task[None] | None:
    """Drop a completed task handle before handling the next client message."""
    if task is not None and task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return None
    return task


async def _cancel_task(task: asyncio.Task[None]) -> None:
    """Cancel a running task and wait until cancellation is observed."""
    if task.done():
        _clear_finished_task(task)
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _send_json(ws: WebSocket, payload: dict[str, Any], lock: asyncio.Lock) -> None:
    """Serialize writes to the same WebSocket."""
    async with lock:
        await ws.send_json(payload)
