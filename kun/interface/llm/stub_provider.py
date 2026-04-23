"""StubProvider — 无网络环境下的确定性测试用 adapter.

用于:
  - 单元测试 (不需要真 LLM)
  - 冷启动校准 (在测试环境跑校准任务)
  - 故障演练 (模拟 API 失败)
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

from kun.interface.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    UsageInfo,
)

ResponseBuilder = Callable[[LLMRequest], LLMResponse]


def _default_builder(request: LLMRequest) -> LLMResponse:
    """Echo the last user message, attributing to the stub."""
    last_user = next((m for m in reversed(request.messages) if m.role == "user"), None)
    content = f"[stub echo] {last_user.content if last_user else ''}"
    return LLMResponse(
        content=content,
        usage=UsageInfo(input_tokens=10, output_tokens=len(content.split())),
        model="stub-1",
        provider="stub",
        tier="cheap",
        finish_reason="stop",
    )


class StubProvider(LLMProvider):
    """Deterministic stub for tests."""

    name = "stub"
    supports_tools = True
    supports_streaming = True
    supports_cache = True

    price_input_per_mtok = 0.0
    price_output_per_mtok = 0.0

    def __init__(
        self,
        *,
        model_id: str = "stub-1",
        tier: ModelTier = "cheap",
        latency_ms: float = 5.0,
        builder: ResponseBuilder | None = None,
        fail_rate: float = 0.0,
    ) -> None:
        self.model_id = model_id
        self.tier = tier
        self.latency_ms = latency_ms
        self._builder = builder or _default_builder
        self._fail_rate = fail_rate
        self._rng = random.Random(42)

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        if self._fail_rate > 0 and self._rng.random() < self._fail_rate:
            raise RuntimeError(f"stub induced failure (rate={self._fail_rate})")
        await asyncio.sleep(self.latency_ms / 1000)
        response = self._builder(request)
        response.latency_ms = (time.perf_counter() - started) * 1000
        # Override identity to reflect the actual provider that served the call
        response.model = self.model_id
        response.provider = self.name
        response.tier = self.tier
        return response

    async def health_check(self) -> bool:
        return True
