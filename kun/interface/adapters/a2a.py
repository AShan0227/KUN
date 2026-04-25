"""A2A JSON-RPC 输出适配器。"""

from __future__ import annotations

import json
from typing import Any

from kun.interface.adapters import register


class A2AAdapter:
    name = "a2a"

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        envelope = {
            "jsonrpc": "2.0",
            "id": context.get("request_id") or payload.get("task_id") or payload.get("id"),
            "method": context.get("method", "task.submit"),
            "params": {
                "recipient_kind": recipient_kind,
                "payload": payload,
                "context": context,
            },
        }
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


register(A2AAdapter())


__all__ = ["A2AAdapter"]
