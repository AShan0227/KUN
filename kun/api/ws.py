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
import hashlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from kun.api.input_payload import (
    Attachment,
    TranslatedInput,
    translate_binary_input,
    translate_chat_input,
)
from kun.api.runtime import get_kill_switch, get_orchestrator
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    resolve_tenant_id,
    tenant_scope,
)
from kun.engineering.orchestrator import OrchestratorEvent
from kun.ops.account_registry import hash_bearer_token, is_token_revoked, record_token_usage
from kun.security.auth import AuthTokenError, extract_bearer_token, verify_bearer_token_any

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
        ctx = await _resolve_ws_tenant_context(ws)
    except AuthTokenError as exc:
        await ws.close(code=1008, reason=str(exc))
        return
    except MissingTenantContextError:
        await ws.close(code=1008, reason="tenant_id or auth_token required")
        return

    await ws.accept()
    raw_audience = (ws.query_params.get("audience") or "developer").lower()
    audience = raw_audience if raw_audience in {"novice", "developer", "expert"} else "developer"
    default_output_kind = str(ws.query_params.get("output_kind") or "user")
    if ctx.audience != audience:
        ctx = TenantContext(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            scopes=ctx.scopes,
            audience=audience,  # type: ignore[arg-type]
        )
    send_lock = asyncio.Lock()
    current_task: asyncio.Task[None] | None = None
    current_task_id: str | None = None
    ks = get_kill_switch(ws.scope["app"])

    log.info("ws.connected", tenant_id=ctx.tenant_id, user_id=ctx.user_id)

    try:
        with tenant_scope(ctx):
            while True:
                current_task = _clear_finished_task(current_task)
                try:
                    msg, translated = await _receive_client_message(ws)
                except ValueError as exc:
                    await _send_json(ws, {"type": "error", "message": str(exc)}, send_lock)
                    continue
                except HTTPException as exc:
                    await _send_json(ws, {"type": "error", "message": str(exc.detail)}, send_lock)
                    continue

                mtype = msg.get("type", "user_message")
                content = (
                    translated.message if translated is not None else str(msg.get("content", ""))
                )
                if translated is not None and translated.descriptors:
                    await _send_json(
                        ws,
                        {"type": "input_detected", "descriptors": translated.descriptors},
                        send_lock,
                    )

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
        log.info("ws.disconnected", tenant_id=ctx.tenant_id)
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


async def _resolve_ws_tenant_context(ws: WebSocket) -> TenantContext:
    """Resolve WebSocket identity.

    Browsers cannot set custom Authorization headers for WebSocket handshakes.
    Production browser clients should first exchange their bearer session for a
    short-lived `/api/auth/ws-ticket`, then pass it as `ws_ticket`.
    Dev can still fall back to tenant/user query params for local dogfood.
    """

    cfg = settings()
    auth_header = ws.headers.get("authorization")
    raw_ws_ticket = ws.query_params.get("ws_ticket") or ws.query_params.get("ticket")
    raw_query_token = (
        raw_ws_ticket
        or ws.query_params.get("auth_token")
        or ws.query_params.get("access_token")
        or ws.query_params.get("token")
    )
    if raw_query_token and not auth_header:
        auth_header = _bearer_header(raw_query_token)

    if auth_header:
        secrets = cfg.auth_secret_candidates()
        if not secrets:
            raise AuthTokenError(
                "KUN_AUTH_SECRET or KUN_AUTH_SECRETS is required for websocket auth"
            )
        claims = verify_bearer_token_any(auth_header, secrets)
        if claims.token_type == "refresh":
            raise AuthTokenError("refresh token cannot open websocket sessions")
        if raw_ws_ticket and claims.token_type != "ws":
            raise AuthTokenError("ws_ticket token_type must be ws")
        if cfg.env == "production" and raw_query_token and not raw_ws_ticket:
            raise AuthTokenError("ws_ticket is required for browser websocket in production")
        if cfg.env == "production":
            revoked = await _check_and_record_ws_auth_token(
                tenant_id=claims.tenant_id,
                auth_header=auth_header,
                ip_hash=_ws_ip_hash(ws),
                user_agent=ws.headers.get("user-agent"),
            )
            if revoked:
                raise AuthTokenError("bearer token revoked")
        return claims.to_tenant_context()

    if cfg.env == "production":
        raise AuthTokenError("auth_token is required for websocket in production")

    return TenantContext(
        tenant_id=resolve_tenant_id(ws.query_params.get("tenant_id")),
        user_id=ws.query_params.get("user_id"),
    )


def _bearer_header(raw_token: str) -> str:
    token = raw_token.strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


async def _check_and_record_ws_auth_token(
    *,
    tenant_id: str,
    auth_header: str,
    ip_hash: str | None,
    user_agent: str | None,
) -> bool:
    token_hash = hash_bearer_token(extract_bearer_token(auth_header))
    async with session_scope(tenant_id=tenant_id) as s:
        revoked = await is_token_revoked(s, tenant_id=tenant_id, token_hash=token_hash)
        if not revoked:
            await record_token_usage(
                s,
                tenant_id=tenant_id,
                token_hash=token_hash,
                ip_hash=ip_hash,
                user_agent=user_agent,
            )
        return revoked


def _ws_ip_hash(ws: WebSocket) -> str | None:
    raw_ip = ws.client.host if ws.client else ""
    cleaned = raw_ip.strip()
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


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


async def _receive_client_message(
    ws: WebSocket,
) -> tuple[dict[str, Any], TranslatedInput | None]:
    packet = await ws.receive()
    if packet["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(code=packet.get("code", 1000))

    raw_bytes = packet.get("bytes")
    if raw_bytes is not None:
        translated = await translate_binary_input(raw_bytes)
        return {"type": "user_message", "content": translated.message}, translated

    raw_text = packet.get("text")
    if raw_text is None:
        raise ValueError("unsupported websocket frame")

    try:
        msg = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON") from exc

    attachments_raw = msg.get("attachments") or []
    if not attachments_raw:
        return msg, None

    attachments = [Attachment.model_validate(item) for item in attachments_raw]
    translated = await translate_chat_input(str(msg.get("content", "")), attachments)
    msg["content"] = translated.message
    return msg, translated


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
