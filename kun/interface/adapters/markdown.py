"""Markdown 报表输出适配器。"""

from __future__ import annotations

import json
from typing import Any

from kun.interface.adapters import register


class MarkdownAdapter:
    name = "markdown"

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        title = str(context.get("title") or payload.get("title") or "KUN 报告")
        lines = [f"# {title}", "", f"- 接收方：`{recipient_kind}`", ""]
        summary = payload.get("summary")
        if summary:
            lines.extend(["## 摘要", "", str(summary), ""])
        lines.extend(
            [
                "## 原始数据",
                "",
                "```json",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
        return "\n".join(lines)


register(MarkdownAdapter())


__all__ = ["MarkdownAdapter"]
