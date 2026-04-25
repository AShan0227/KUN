"""REST 请求 body 输出适配器。"""

from __future__ import annotations

import json
from typing import Any

from kun.interface.adapters import register


class RESTAdapter:
    name = "rest"

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        body = {
            "method": context.get("method", "POST"),
            "path": context.get("path", "/"),
            "headers": context.get("headers", {}),
            "recipient_kind": recipient_kind,
            "body": payload,
        }
        return json.dumps(body, ensure_ascii=False, sort_keys=True)


register(RESTAdapter())


__all__ = ["RESTAdapter"]
