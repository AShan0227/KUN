"""WebSocket runtime helper tests."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from kun.api.ws import _cancel_task, _clear_finished_task, _receive_client_message


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
    def __init__(self, packet: dict) -> None:
        self._packet = packet

    async def receive(self) -> dict:
        return self._packet


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
