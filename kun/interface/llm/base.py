"""LLM Provider abstraction.

Router layer sees only capability tags (tier / strength / cost / latency),
not vendor specifics. Vendor switch = swap an adapter, no business code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

LLMRole = Literal["system", "user", "assistant", "tool"]

ModelTier = Literal[
    "top",  # Opus 4.7 档 (意图 / 复杂)
    "strong",  # Sonnet 4.6 档 (中档)
    "coding",  # Codex 5.3 (编程专项)
    "cheap",  # Haiku 4.5 (路由 / 分类 / 简单判官)
    "fallback",  # MiniMax M2.7 (兜底)
]


class LLMMessage(BaseModel):
    role: LLMRole
    content: str
    name: str | None = None  # For tool results
    tool_call_id: str | None = None
    cache: bool = Field(
        default=False,
        description="Mark this message for prompt caching (ADR permanent/stable segment)",
    )


class ToolSpec(BaseModel):
    """A tool the model can call."""

    name: str
    description: str
    schema_: dict[str, Any] = Field(alias="schema", default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class UsageInfo(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class TaskProfile(BaseModel):
    """Hints about a task, used by the router to pick tier."""

    task_type: str = ""
    risk_level: str = "low"
    needs_coding: bool = False
    needs_creative: bool = False
    needs_reasoning: bool = False
    max_cost_usd: float | None = None
    prefer_speed: bool = False


class LLMRequest(BaseModel):
    """One LLM call."""

    model_config = ConfigDict(extra="forbid")

    messages: list[LLMMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    temperature: float = 0.7
    max_tokens: int = 2048
    stop: list[str] = Field(default_factory=list)
    # Optional profile used by the router
    profile: TaskProfile | None = None
    # Stream or not (streaming not used for every call, but supported)
    stream: bool = False


class LLMResponse(BaseModel):
    """Result of one call."""

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    model: str = ""
    provider: str = ""
    tier: ModelTier = "top"
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    latency_ms: float = 0.0
    finish_reason: Literal["stop", "tool_use", "length", "error"] = "stop"
    # Original vendor response (optional, for debugging)
    raw: dict[str, Any] | None = None


class LLMProvider(ABC):
    """Provider interface. Implement one per vendor."""

    name: str  # e.g. "anthropic"
    model_id: str  # e.g. "claude-opus-4-7"
    tier: ModelTier

    # Per-token prices (USD per million tokens) — for cost_usd_actual
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    price_cached_per_mtok: float = 0.0

    # For subscription models, equivalent pricing (ADR-008)
    equivalent_price_input_per_mtok: float = 0.0
    equivalent_price_output_per_mtok: float = 0.0

    # Capabilities
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_cache: bool = False

    @abstractmethod
    async def invoke(self, request: LLMRequest) -> LLMResponse:
        """Execute a single non-streaming call."""

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        """Stream tokens. Default naive fallback: call invoke() and yield once."""
        response = await self.invoke(request)
        yield response.content

    # ---- helpers ----

    def compute_cost(self, usage: UsageInfo, *, equivalent: bool = False) -> float:
        """Compute cost from usage + price. ADR-008: equivalent vs actual."""
        if equivalent:
            pin = self.equivalent_price_input_per_mtok or self.price_input_per_mtok
            pout = self.equivalent_price_output_per_mtok or self.price_output_per_mtok
        else:
            pin = self.price_input_per_mtok
            pout = self.price_output_per_mtok

        input_cost = (usage.input_tokens / 1_000_000) * pin
        # Cached tokens at a discount
        cache_cost = (usage.cached_input_tokens / 1_000_000) * self.price_cached_per_mtok
        output_cost = (usage.output_tokens / 1_000_000) * pout
        return input_cost + cache_cost + output_cost

    async def health_check(self) -> bool:
        """Quick probe: is the provider up & reachable?"""
        try:
            await self.invoke(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="ping")],
                    max_tokens=4,
                )
            )
            return True
        except Exception:
            return False
