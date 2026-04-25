"""面向人的自然语言输出适配器。"""

from __future__ import annotations

import json
from typing import Any

from kun.interface.adapters import register
from kun.interface.llm import LLMMessage, LLMRequest, LLMRouter, get_router

_HUMAN_SYSTEM = """你是 KUN 的输出翻译器。
把内部结构化 JSON 改写成用户能看懂的自然语言。
要求：
- 用大白话
- 只说和用户有关的信息
- 不泄露内部无关字段
- 按 context.language 决定中英文，默认中文
"""


class HumanAdapter:
    name = "human"

    def __init__(self, router: LLMRouter | None = None) -> None:
        self._router = router

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        context = context or {}
        router = self._router or get_router()
        prompt = (
            f"recipient_kind={recipient_kind}\n"
            f"context={json.dumps(context, ensure_ascii=False, sort_keys=True)}\n"
            f"payload={json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )
        response = await router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_HUMAN_SYSTEM, cache=True),
                    LLMMessage(role="user", content=prompt),
                ],
                temperature=0.2,
                max_tokens=512,
            ),
            purpose="classification",
        )
        return response.content.strip()


register(HumanAdapter())


__all__ = ["HumanAdapter"]
