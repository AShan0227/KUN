"""Anthropic provider adapter (for Opus 4.7 / Sonnet 4.6 / Haiku 4.5).

Authentication: we support three modes (ADR-002):
  1. via ofox proxy (subscription) — set KUN_OFOX_API_KEY + KUN_OFOX_PROXY_URL
  2. direct Anthropic API key — set ANTHROPIC_API_KEY=sk-ant-api03-...
     Uses standard ``x-api-key`` header. Metered (per-token billing).
  3. OAuth subscription token — set ANTHROPIC_API_KEY=sk-ant-oat01-...
     Uses ``Authorization: Bearer <token>`` + ``anthropic-beta: oauth-2025-04-20``.
     This is what ``claude setup-token`` emits. Consumes the user's Claude
     Pro/Max subscription quota instead of paying per-token. Useful when the
     KUN runtime needs to share a developer's existing subscription rather
     than a metered API key.

All three routes go through the anthropic SDK; ofox just overrides ``base_url``,
and OAuth flips ``api_key``→``auth_token`` so the SDK sends Bearer auth.
"""

from __future__ import annotations

import os
import time
from typing import Any, Literal

from anthropic import AsyncAnthropic

from kun.core.config import settings
from kun.core.logging import get_logger
from kun.core.metrics import llm_cost_usd, llm_latency_seconds, llm_request_total
from kun.interface.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    ToolCall,
    UsageInfo,
)

log = get_logger("kun.llm.anthropic")


# Pricing in USD per million tokens. Source: Anthropic pricing table (claude-api
# skill, 2026-06). cached = cache *read* (~0.1x input); cache_write = 5-min-TTL
# cache *write* (1.25x input). Audit F024: the old table had Opus at $15/$75
# (3x too high) and Haiku at $0.25/$1.25 (4x too low), and omitted cache-write —
# corrupting the ADR-008 cost loop and budget kill-switch.
_PRICING: dict[str, dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cached": 1.0, "cache_write": 12.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cached": 0.5, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cached": 0.5, "cache_write": 6.25},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cached": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cached": 0.1, "cache_write": 1.25},
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cached": 0.1,
        "cache_write": 1.25,
    },
}


# Model families that REMOVED sampling params — sending `temperature` 400s
# (claude-api skill, 2026-06): Fable 5 / Mythos 5 / Opus 4.7 / Opus 4.8. Matched
# as substrings of the model id. Audit F122: the old check only listed
# "opus-4-7", so opus-4-8 / fable-5 would still send temperature and 400.
_NO_SAMPLING_PARAM_MODELS: tuple[str, ...] = (
    "opus-4-7",
    "opus-4-8",
    "fable-5",
    "mythos-5",
    "mythos-preview",
)


def _accepts_temperature(model_id: str) -> bool:
    """Whether this model still accepts the `temperature` sampling param."""
    return not any(s in model_id for s in _NO_SAMPLING_PARAM_MODELS)


def _to_anthropic_message(m: LLMMessage) -> dict[str, Any]:
    """Map one LLMMessage to an Anthropic ``messages[]`` entry (audit F044).

    ``role="tool"`` is NOT a valid Anthropic role — the API accepts only
    user/assistant/system. Tool results must be a ``tool_result`` content block
    inside a **user** message, keyed by the originating ``tool_use_id``. The old
    code passed ``{"role": "tool", ...}`` straight through, so any multi-turn tool
    loop returned a 400. (system is filtered out before this is called.)
    """
    if m.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content,
                }
            ],
        }
    if m.cache:
        # Wrap as cache_control for prompt caching.
        return {
            "role": m.role,
            "content": [
                {"type": "text", "text": m.content, "cache_control": {"type": "ephemeral"}}
            ],
        }
    return {"role": m.role, "content": m.content}


def _map_finish_reason(
    stop_reason: str | None, *, has_tool_calls: bool
) -> Literal["stop", "tool_use", "length", "error"]:
    """Map Anthropic stop_reason → LLMResponse.finish_reason (audit F124).

    The old mapping folded everything except max_tokens/tool_use into "stop", so
    a ``refusal`` (safety decline) or ``model_context_window_exceeded`` was
    reported as a normal successful stop — a failure masquerading as success.
    """
    if has_tool_calls:
        return "tool_use"
    if stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return "length"
    if stop_reason == "refusal":
        return "error"
    return "stop"


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
        # Cache-write premium; default to 1.25x input for unknown models.
        self.price_cache_write_per_mtok = pricing.get(
            "cache_write", self.price_input_per_mtok * 1.25
        )

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
            # OAuth subscription token: ``sk-ant-oat...`` from ``claude setup-token``.
            # The SDK normally sends ``x-api-key: <key>``; for OAuth the server
            # rejects that and requires ``Authorization: Bearer <token>`` plus
            # the ``anthropic-beta: oauth-2025-04-20`` opt-in header.
            #
            # We pass the token as ``auth_token`` (so the SDK's ``_bearer_auth``
            # builds the Bearer header) and explicitly null ``client.api_key``
            # after construction — otherwise the SDK auto-picks up the same
            # value from the ``ANTHROPIC_API_KEY`` env var and sends BOTH
            # ``X-Api-Key`` and ``Authorization`` headers, which the server
            # rejects.
            if direct_key.startswith("sk-ant-oat"):
                log.info(
                    "anthropic.oauth_mode",
                    token_prefix=direct_key[:14],
                    model=self.model_id,
                )
                client = AsyncAnthropic(
                    auth_token=direct_key,
                    default_headers={"anthropic-beta": "oauth-2025-04-20"},
                )
                # Suppress the env-var-derived api_key so only Bearer goes out.
                client.api_key = None
                return client
            return AsyncAnthropic(api_key=direct_key)
        # No credentials — fail fast on call
        log.warning("anthropic.no_credentials", hint="set KUN_OFOX_API_KEY or ANTHROPIC_API_KEY")
        return AsyncAnthropic(api_key="missing")

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()

        # Split system from rest (Anthropic-specific API shape)
        system_text = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages: list[dict[str, Any]] = [
            _to_anthropic_message(m) for m in request.messages if m.role != "system"
        ]

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        # Fable 5 / Opus 4.7 / 4.8 removed `temperature` and 400 if it is sent
        # (audit F122). Only include it for models that still accept it.
        if _accepts_temperature(self.model_id):
            kwargs["temperature"] = request.temperature
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

        finish_reason = _map_finish_reason(resp.stop_reason, has_tool_calls=bool(tool_calls))

        # Metrics
        llm_request_total.labels(
            provider=self.name,
            model=self.model_id,
            role="invoke",
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
