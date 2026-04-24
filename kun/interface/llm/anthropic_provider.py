"""Anthropic provider adapter (for Opus 4.7 / Sonnet 4.6 / Haiku 4.5).

Authentication: we support two modes (ADR-002):
  1. via ofox proxy (subscription) — set KUN_OFOX_API_KEY + KUN_OFOX_PROXY_URL
  2. direct Anthropic API — set ANTHROPIC_API_KEY

Both route through the anthropic SDK; the proxy option just overrides base_url.
"""

from __future__ import annotations

import os
import time
from typing import Any

from anthropic import AsyncAnthropic

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

log = get_logger("kun.llm.anthropic")


# Pricing in USD per million tokens (approximate; update periodically)
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cached": 1.5,  # 90% discount on cache hit
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cached": 0.3,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.25,
        "output": 1.25,
        "cached": 0.025,
    },
}


class AnthropicProvider(LLMProvider):
    """Adapter for Claude Opus / Sonnet / Haiku."""

    name = "anthropic"
    supports_tools = True
    supports_streaming = True
    supports_cache = True

    def __init__(self, model_id: str, tier: ModelTier) -> None:
        self.model_id = model_id
        self.tier = tier
        self._client = self._build_client()

        pricing = _PRICING.get(model_id, {})
        self.price_input_per_mtok = pricing.get("input", 3.0)
        self.price_output_per_mtok = pricing.get("output", 15.0)
        self.price_cached_per_mtok = pricing.get("cached", 0.3)

        # For ADR-008 equivalent pricing — same as actual for now
        self.equivalent_price_input_per_mtok = self.price_input_per_mtok
        self.equivalent_price_output_per_mtok = self.price_output_per_mtok

    def _build_client(self) -> AsyncAnthropic:
        cfg = settings()
        # Prefer ofox proxy if API key present
        if cfg.ofox_api_key:
            return AsyncAnthropic(
                api_key=cfg.ofox_api_key,
                base_url=cfg.ofox_proxy_url,
            )
        direct_key = os.getenv("ANTHROPIC_API_KEY")
        if direct_key:
            return AsyncAnthropic(api_key=direct_key)
        # No credentials — fail fast on call
        log.warning("anthropic.no_credentials", hint="set KUN_OFOX_API_KEY or ANTHROPIC_API_KEY")
        return AsyncAnthropic(api_key="missing")

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()

        # Split system from rest (Anthropic-specific API shape)
        system_text = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages: list[dict[str, Any]] = []
        for m in request.messages:
            if m.role == "system":
                continue
            block: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.cache:
                # Wrap as cache_control for prompt caching
                block = {
                    "role": m.role,
                    "content": [
                        {
                            "type": "text",
                            "text": m.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            messages.append(block)

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system_text:
            kwargs["system"] = system_text
        if request.stop:
            kwargs["stop_sequences"] = request.stop
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.schema_ or {"type": "object", "properties": {}},
                }
                for t in request.tools
            ]

        resp = await self._client.messages.create(**kwargs)

        latency = (time.perf_counter() - started) * 1000

        # Extract content + tool calls
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                content_parts.append(str(getattr(block, "text", "")))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=getattr(block, "input", {}),
                    )
                )

        usage = UsageInfo(
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            cached_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=(
                getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            ),
        )

        cost_actual = self.compute_cost(usage, equivalent=False)
        cost_equiv = self.compute_cost(usage, equivalent=True)

        finish_reason = (
            "tool_use" if tool_calls else ("length" if resp.stop_reason == "max_tokens" else "stop")
        )

        # Metrics
        llm_request_total.labels(
            provider=self.name,
            model=self.model_id,
            role="invoke",
            tenant_id="unknown",
        ).inc()
        llm_latency_seconds.labels(provider=self.name, model=self.model_id).observe(latency / 1000)
        llm_cost_usd.labels(provider=self.name, model=self.model_id, tenant_id="unknown").inc(
            cost_actual
        )

        return LLMResponse(
            content="".join(content_parts),
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
