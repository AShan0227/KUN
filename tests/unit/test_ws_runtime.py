"""WebSocket runtime helper tests."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from kun.api import ws as ws_module
from kun.api.ws import (
    _cancel_task,
    _clear_finished_task,
    _receive_client_message,
    _resolve_ws_tenant_context,
)
from kun.security.auth import AuthTokenError, sign_auth_token


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_task_stops_running_task() -> None:
    cancelled = asyncio.Event()

    async def run_forever() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(run_forever())
    await asyncio.sleep(0)

    await _cancel_task(task)

    assert task.cancelled()
    assert cancelled.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_finished_task_returns_none_for_completed_task() -> None:
    async def done() -> None:
        return None

    task = asyncio.create_task(done())
    await task

    assert _clear_finished_task(task) is None


class _FakeWebSocket:
    def __init__(self, packet: dict[str, Any]) -> None:
        self._packet = packet

    async def receive(self) -> dict[str, Any]:
        return self._packet


class _FakeHandshake:
    def __init__(
        self,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.query_params = query or {}
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")


class _Settings:
    def __init__(self, *, env: str = "dev", secrets: list[str] | None = None) -> None:
        self.env = env
        self._secrets = secrets or []

    def auth_secret_candidates(self) -> list[str]:
        return self._secrets


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receive_client_message_translates_json_attachments() -> None:
    packet = {
        "type": "websocket.receive",
        "text": json.dumps(
            {
                "type": "user_message",
                "content": "处理附件",
                "attachments": [
                    {
                        "filename": "note.txt",
                        "content_b64": base64.b64encode(b"hello ws").decode("ascii"),
                    }
                ],
            }
        ),
    }

    msg, translated = await _receive_client_message(_FakeWebSocket(packet))  # type: ignore[arg-type]

    assert msg["type"] == "user_message"
    assert "hello ws" in msg["content"]
    assert translated is not None
    assert translated.descriptors[0]["filename"] == "note.txt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receive_client_message_translates_binary_frame() -> None:
    packet = {"type": "websocket.receive", "bytes": b"binary text"}

    msg, translated = await _receive_client_message(_FakeWebSocket(packet))  # type: ignore[arg-type]

    assert msg["type"] == "user_message"
    assert "binary text" in msg["content"]
    assert translated is not None
    assert translated.descriptors[0]["filename"] == "websocket.bin"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_context_uses_signed_query_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "x" * 32
    token = sign_auth_token(
        {
            "tenant_id": "tenant-token",
            "user_id": "user-token",
            "scopes": ["world:approve"],
        },
        secret,
    )
    monkeypatch.setattr(ws_module, "settings", lambda: _Settings(env="staging", secrets=[secret]))

    ctx = await _resolve_ws_tenant_context(
        cast(Any, _FakeHandshake(query={"auth_token": token, "tenant_id": "ignored"}))
    )

    assert ctx.tenant_id == "tenant-token"
    assert ctx.user_id == "user-token"
    assert ctx.scopes == ("world:approve",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_context_uses_short_lived_ws_ticket_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "x" * 32
    token = sign_auth_token(
        {
            "tenant_id": "tenant-ticket",
            "user_id": "user-ticket",
            "scopes": ["chat:write"],
            "token_type": "ws",
        },
        secret,
    )
    monkeypatch.setattr(
        ws_module,
        "settings",
        lambda: _Settings(env="production", secrets=[secret]),
    )

    async def fake_check_and_record(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(ws_module, "_check_and_record_ws_auth_token", fake_check_and_record)

    ctx = await _resolve_ws_tenant_context(cast(Any, _FakeHandshake(query={"ws_ticket": token})))

    assert ctx.tenant_id == "tenant-ticket"
    assert ctx.user_id == "user-ticket"
    assert ctx.scopes == ("chat:write",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_context_rejects_access_token_query_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "x" * 32
    token = sign_auth_token(
        {
            "tenant_id": "tenant-access",
            "user_id": "user-access",
            "token_type": "access",
        },
        secret,
    )
    monkeypatch.setattr(
        ws_module,
        "settings",
        lambda: _Settings(env="production", secrets=[secret]),
    )

    with pytest.raises(AuthTokenError, match="ws_ticket is required"):
        await _resolve_ws_tenant_context(cast(Any, _FakeHandshake(query={"auth_token": token})))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ws_context_requires_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ws_module,
        "settings",
        lambda: _Settings(env="production", secrets=["x" * 32]),
    )

    with pytest.raises(AuthTokenError, match="auth_token is required"):
        await _resolve_ws_tenant_context(
            cast(Any, _FakeHandshake(query={"tenant_id": "tenant-dev", "user_id": "user-dev"}))
        )
