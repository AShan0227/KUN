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


Audience = Literal["novice", "developer", "expert"]


class TaskProfile(BaseModel):
    """Hints about a task, used by the router to pick tier and shape output.

    Three knobs the caller can tune:
      - **risk_level / needs_***: routing strength (router maps these to tier)
      - **max_cost_usd / max_duration_sec**: hard ceilings the orchestrator enforces
      - **audience**: shapes the system prompt — novice / developer / expert
    """

    task_type: str = ""
    risk_level: str = "low"
    needs_coding: bool = False
    needs_creative: bool = False
    needs_reasoning: bool = False
    max_cost_usd: float | None = None
    max_duration_sec: float | None = Field(
        default=None,
        description=(
            "Hard cap on total task duration in seconds. None = use the global "
            "default (KUN_TASK_MAX_DURATION_SEC, 1800s). Overflow → orchestrator "
            "cancels and emits task.timed_out."
        ),
    )
    prefer_speed: bool = False
    audience: Audience = Field(
        default="developer",
        description=(
            "Who's reading the answer — controls the assistant's voice. "
            "novice = plain language, no jargon. "
            "developer = concise, code/paths welcome. "
            "expert = depth, alternatives, trade-offs."
        ),
    )
    force_fallback: bool = Field(
        default=False,
        description=(
            "Set by the orchestrator when daily budget is exceeded — router "
            "must skip top/strong/cheap and route everything to the cheap "
            "fallback (MiniMax) instead of letting the subscription pile up."
        ),
    )


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
    # V2.2 §22 Wire 11: hermes 结构化执行 — 强制 LLM JSON output schema
    # provider 看到这字段就启用 strict mode (Anthropic tool calling 模拟 / OpenAI
    # response_format json_schema). 不支持的 provider 静默降级到 prompt-only.
    response_format: dict[str, Any] | None = None


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
    route_debug: dict[str, Any] = Field(
        default_factory=dict,
        description="Router rationale snapshot for DecisionTicket / StateLedger.",
    )
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
