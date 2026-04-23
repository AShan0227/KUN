"""MiniMax M2.7 provider adapter (ADR-002 fallback)."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from kun.core.logging import get_logger
from kun.core.metrics import llm_cost_usd, llm_latency_seconds, llm_request_total
from kun.interface.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    ToolCall,
    UsageInfo,
)

log = get_logger("kun.llm.minimax")


class MiniMaxProvider(LLMProvider):
    """MiniMax chat completion adapter. OpenAI-compatible endpoint."""

    name = "minimax"
    tier: ModelTier = "fallback"
    supports_tools = True
    supports_streaming = False
    supports_cache = False

    # MiniMax M2.7 published pricing (approximate, adjust with reality)
    price_input_per_mtok = 0.4
    price_output_per_mtok = 1.2

    # Equivalent prices same as actual (MiniMax is pay-per-token)
    equivalent_price_input_per_mtok = 0.4
    equivalent_price_output_per_mtok = 1.2

    def __init__(
        self,
        model_id: str = "minimax-m2.7",
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.base_url = base_url or os.getenv("MINIMAX_API_URL", "https://api.minimax.chat/v1")
        self.api_key = api_key or os.getenv("MINIMAX_API_KEY", "")

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()

        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop:
            payload["stop"] = request.stop
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema_ or {"type": "object", "properties": {}},
                    },
                }
                for t in request.tools
            ]
            payload["tool_choice"] = "auto"

        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        latency = (time.perf_counter() - started) * 1000

        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""

        tool_calls: list[ToolCall] = []
        if message.get("tool_calls"):
            import json

            for tc in message["tool_calls"]:
                args_raw = tc.get("function", {}).get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=args,
                    )
                )

        u = data.get("usage", {})
        usage = UsageInfo(
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
        )
        cost_actual = self.compute_cost(usage, equivalent=False)
        cost_equiv = self.compute_cost(usage, equivalent=True)

        finish_reason = (
            "tool_use"
            if tool_calls
            else ("length" if choice.get("finish_reason") == "length" else "stop")
        )

        llm_request_total.labels(
            provider=self.name, model=self.model_id, role="invoke", tenant_id="unknown"
        ).inc()
        llm_latency_seconds.labels(provider=self.name, model=self.model_id).observe(latency / 1000)
        llm_cost_usd.labels(provider=self.name, model=self.model_id, tenant_id="unknown").inc(
            cost_actual
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=self.model_id,
            provider=self.name,
            tier=self.tier,
            cost_usd_actual=cost_actual,
            cost_usd_equivalent=cost_equiv,
            latency_ms=latency,
            finish_reason=finish_reason,
        )
