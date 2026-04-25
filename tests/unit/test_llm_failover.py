"""LLM provider 故障转移测试。"""

from __future__ import annotations

import pytest
from kun.interface.llm import LLMMessage, LLMProvider, LLMRequest, LLMResponse, ModelTier
from kun.interface.llm.failover import FailoverGuard, FailoverPolicy
from kun.interface.llm.router import LLMRouter


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeProvider(LLMProvider):
    supports_tools = False
    supports_streaming = False

    def __init__(self, *, name: str, tier: ModelTier, fail: bool = False) -> None:
        self.name = name
        self.model_id = name
        self.tier = tier
        self.fail = fail
        self.calls = 0

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return LLMResponse(
            content=self.name, provider=self.name, model=self.model_id, tier=self.tier
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failover_guard_threshold_and_cooldown() -> None:
    clock = _Clock()
    guard = FailoverGuard(
        FailoverPolicy(primary="primary", backup=["backup"], failure_threshold=2, cooldown_sec=10),
        tier_provider_names={"top": "primary", "fallback": "backup"},
        clock=clock,
    )

    assert await guard.current_active() == "primary"
    assert await guard.record_failure("primary", tier="top") is False
    assert await guard.current_active() == "primary"

    assert await guard.record_failure("primary", tier="top") is True
    assert await guard.current_active() == "backup"
    assert await guard.is_available("primary", tier="top") is False

    clock.advance(11)
    assert await guard.is_available("primary", tier="top") is True
    assert await guard.current_active() == "primary"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failover_guard_walks_multiple_backups() -> None:
    guard = FailoverGuard(
        FailoverPolicy(primary="a", backup=["b", "c"], failure_threshold=1, cooldown_sec=300),
        tier_provider_names={"top": "a", "strong": "b", "fallback": "c"},
    )

    assert await guard.record_failure("a", tier="top") is True
    assert await guard.current_active() == "b"

    assert await guard.record_failure("b", tier="strong") is True
    assert await guard.current_active() == "c"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failover_guard_tracks_failures_per_tier() -> None:
    guard = FailoverGuard(
        FailoverPolicy(
            primary="claude-code-cli",
            backup=["minimax"],
            failure_threshold=1,
            cooldown_sec=300,
        ),
        tier_provider_names={
            "top": "claude-code-cli",
            "cheap": "claude-code-cli",
            "fallback": "minimax",
        },
    )

    assert await guard.record_failure("claude-code-cli", tier="top") is True
    await guard.record_success("claude-code-cli", tier="cheap")

    assert await guard.is_available("claude-code-cli", tier="top") is False
    assert await guard.is_available("claude-code-cli", tier="cheap") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_skips_provider_after_threshold() -> None:
    top = _FakeProvider(name="primary", tier="top", fail=True)
    backup = _FakeProvider(name="backup", tier="fallback", fail=False)
    guard = FailoverGuard(
        FailoverPolicy(primary="primary", backup=["backup"], failure_threshold=1, cooldown_sec=300),
        tier_provider_names={"top": "primary", "fallback": "backup"},
    )
    router = LLMRouter({"top": top, "fallback": backup}, failover_guard=guard)

    request = LLMRequest(messages=[LLMMessage(role="user", content="hello")])
    first = await router.invoke(request, purpose="execution")
    top_calls_after_first_failure = top.calls
    second = await router.invoke(request, purpose="execution")

    assert first.provider == "backup"
    assert second.provider == "backup"
    assert top.calls == top_calls_after_first_failure
    assert backup.calls == 2
