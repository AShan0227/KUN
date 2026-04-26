"""OpenAI provider adapter (for Codex 5.3 / GPT-5 etc).

Authentication via OPENAI_API_KEY or the ofox proxy.
"""

from __future__ import annotations

import os
import time
from typing import Any

from openai import AsyncOpenAI

from kun.core.config import settings
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

log = get_logger("kun.llm.openai")


# Approximate pricing (USD per million tokens) — update periodically.
_PRICING: dict[str, dict[str, float]] = {
    "codex-5.3": {"input": 5.0, "output": 25.0, "cached": 0.5},
    "gpt-5": {"input": 10.0, "output": 40.0, "cached": 1.0},
    "gpt-5-mini": {"input": 0.5, "output": 2.0, "cached": 0.05},
}


class OpenAIProvider(LLMProvider):
    """Adapter for OpenAI-family models."""

    name = "openai"
    supports_tools = True
    supports_streaming = True
    supports_cache = True

    def __init__(self, model_id: str, tier: ModelTier) -> None:
        self.model_id = model_id
        self.tier = tier
        self._client = self._build_client()

        pricing = _PRICING.get(model_id, {})
        self.price_input_per_mtok = pricing.get("input", 5.0)
        self.price_output_per_mtok = pricing.get("output", 25.0)
        self.price_cached_per_mtok = pricing.get("cached", 0.5)
        self.equivalent_price_input_per_mtok = self.price_input_per_mtok
        self.equivalent_price_output_per_mtok = self.price_output_per_mtok

    def _build_client(self) -> AsyncOpenAI:
        cfg = settings()
        # Prefer ofox proxy if configured
        if cfg.ofox_api_key:
            return AsyncOpenAI(
                api_key=cfg.ofox_api_key,
                base_url=f"{cfg.ofox_proxy_url}/openai/v1",
            )
        direct_key = os.getenv("OPENAI_API_KEY")
        if direct_key:
            return AsyncOpenAI(api_key=direct_key)
        log.warning("openai.no_credentials")
        return AsyncOpenAI(api_key="missing")

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()

        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop:
            kwargs["stop"] = request.stop
        if request.tools:
            kwargs["tools"] = [
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
            kwargs["tool_choice"] = "auto"

        # V2.2 §22 Wire 11: response_format strict mode (OpenAI 原生支持)
        # 接受 {"type": "json_schema", "json_schema": {...}} (OpenAI 4o 原生格式)
        # 或 {"type": "json_object"} (旧格式)
        if request.response_format:
            kwargs["response_format"] = request.response_format

        resp = await self._client.chat.completions.create(**kwargs)
        latency = (time.perf_counter() - started) * 1000

        choice = resp.choices[0]
        content = choice.message.content or ""
        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            import json

            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments or "{}"),
                    )
                )

        u = resp.usage
        usage = UsageInfo(
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            cached_input_tokens=(
                getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            ),
        )
        cost_actual = self.compute_cost(usage, equivalent=False)
        cost_equiv = self.compute_cost(usage, equivalent=True)

        finish_reason = (
            "tool_use" if tool_calls else ("length" if choice.finish_reason == "length" else "stop")
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
