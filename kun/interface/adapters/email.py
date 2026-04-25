"""HTML 邮件输出适配器。"""

from __future__ import annotations

import html
import json
from typing import Any

from kun.interface.adapters import register


class EmailAdapter:
    name = "email"

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        title = html.escape(str(context.get("subject") or payload.get("title") or "KUN 更新"))
        body = html.escape(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        recipient = html.escape(recipient_kind)
        return (
            "<!doctype html><html><body>"
            f"<h2>{title}</h2>"
            f"<p>Recipient: {recipient}</p>"
            f"<pre>{body}</pre>"
            "</body></html>"
        )


register(EmailAdapter())


__all__ = ["EmailAdapter"]
