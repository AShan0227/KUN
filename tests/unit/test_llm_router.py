"""Router tier decision + fallback tests."""

import pytest
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMRouter,
    TaskProfile,
    get_router,
)
from kun.interface.llm.router import reset_router
from kun.interface.llm.stub_provider import StubProvider


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purpose_maps_to_tier():
    providers = {
        "top": StubProvider(model_id="top", tier="top"),
        "cheap": StubProvider(model_id="cheap", tier="cheap"),
        "coding": StubProvider(model_id="coding", tier="coding"),
        "fallback": StubProvider(model_id="fb", tier="fallback"),
    }
    router = LLMRouter(providers)
    decision = router.decide("intent")
    assert decision.primary_tier == "top"

    decision = router.decide("classification")
    assert decision.primary_tier == "cheap"

    decision = router.decide("coding")
    assert decision.primary_tier == "coding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_profile_override_coding():
    providers = {
        "top": StubProvider(model_id="top", tier="top"),
        "coding": StubProvider(model_id="coding", tier="coding"),
        "fallback": StubProvider(model_id="fb", tier="fallback"),
    }
    router = LLMRouter(providers)
    decision = router.decide("execution", TaskProfile(needs_coding=True))
    assert decision.primary_tier == "coding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_triggers_on_failure():
    providers = {
        "top": StubProvider(model_id="flaky-top", tier="top", fail_rate=1.0),
        "fallback": StubProvider(model_id="reliable-fb", tier="fallback"),
    }
    router = LLMRouter(providers)
    response = await router.invoke(
        LLMRequest(messages=[LLMMessage(role="user", content="x" * 3500)]),
        purpose="execution",
    )
    assert response.provider == "stub"
    assert response.tier == "fallback"
    assert response.route_debug["initial_tier"] == "top"
    assert response.route_debug["fallback_engaged"] is True
    assert response.route_debug["primary_error"] == "RetryError"
    assert response.route_debug["final_tier"] == "fallback"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_critical_risk_forces_top():
    providers = {
        "top": StubProvider(model_id="top", tier="top"),
        "cheap": StubProvider(model_id="cheap", tier="cheap"),
        "fallback": StubProvider(model_id="fb", tier="fallback"),
    }
    router = LLMRouter(providers)
    profile = TaskProfile(risk_level="critical")
    decision = router.decide("classification", profile)
    assert decision.primary_tier == "top"


# ============== A/B 切流 (router 第二候选) ==============


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_ab_disabled_always_uses_primary(monkeypatch):
    """没配 alternates / ratio=0 → 永远走 primary, 不切流."""
    from kun.interface.llm.base import LLMResponse

    primary = StubProvider(model_id="primary-top", tier="top")
    challenger = StubProvider(model_id="challenger-top", tier="top")
    providers = {"top": primary, "fallback": StubProvider(model_id="fb", tier="fallback")}

    # ratio=0 关闭
    router = LLMRouter(providers, ab_alternates={"top": challenger}, ab_ratio=0.0)
    # 即使 roll=0 (确定切流) ratio=0 也不让切
    monkeypatch.setattr("kun.interface.llm.router._ab_roll", lambda: 0.0)
    response: LLMResponse = await router.invoke(
        LLMRequest(
            messages=[LLMMessage(role="user", content="x" * 3500)],
        ),
        purpose="execution",
    )
    assert response.model == "primary-top"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_ab_below_threshold_uses_primary(monkeypatch):
    """roll(0.5) > ratio(0.1) → 走 primary."""
    primary = StubProvider(model_id="primary-top", tier="top")
    challenger = StubProvider(model_id="challenger-top", tier="top")
    providers = {"top": primary, "fallback": StubProvider(model_id="fb", tier="fallback")}

    router = LLMRouter(providers, ab_alternates={"top": challenger}, ab_ratio=0.1)
    monkeypatch.setattr("kun.interface.llm.router._ab_roll", lambda: 0.5)
    response = await router.invoke(
        LLMRequest(messages=[LLMMessage(role="user", content="x" * 3500)]),
        purpose="execution",
    )
    assert response.model == "primary-top"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_ab_above_threshold_uses_challenger(monkeypatch):
    """roll(0.05) < ratio(0.5) → 走 challenger."""
    primary = StubProvider(model_id="primary-top", tier="top")
    challenger = StubProvider(model_id="challenger-top", tier="top")
    providers = {"top": primary, "fallback": StubProvider(model_id="fb", tier="fallback")}

    router = LLMRouter(providers, ab_alternates={"top": challenger}, ab_ratio=0.5)
    monkeypatch.setattr("kun.interface.llm.router._ab_roll", lambda: 0.05)
    response = await router.invoke(
        LLMRequest(messages=[LLMMessage(role="user", content="x" * 3500)]),
        purpose="execution",
    )
    assert response.model == "challenger-top"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_ab_ratio_clamped_to_unit_interval():
    """ab_ratio 超出 [0, 1] → 自动夹回去, 不抛."""
    primary = StubProvider(model_id="primary", tier="top")
    providers = {"top": primary, "fallback": StubProvider(model_id="fb", tier="fallback")}
    router = LLMRouter(providers, ab_ratio=2.5)
    assert router.ab_ratio == 1.0
    router2 = LLMRouter(providers, ab_ratio=-0.5)
    assert router2.ab_ratio == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_credit_can_upgrade_real_hot_path(monkeypatch):
    """历史信用差距明显时, invoke 真会改 primary_tier, 不只写报告."""

    from kun.engineering.credit_assignment import get_contribution_tracker

    get_contribution_tracker().reset()
    providers = {
        "top": StubProvider(model_id="model-top", tier="top"),
        "strong": StubProvider(model_id="model-strong", tier="strong"),
        "cheap": StubProvider(model_id="model-cheap", tier="cheap"),
        "fallback": StubProvider(model_id="model-fallback", tier="fallback"),
    }
    router = LLMRouter(providers)

    async def fake_load_scores(resource_keys: list[str]) -> dict[str, float]:
        assert "model_tier:strong" in resource_keys
        return {"model:strong-model": 0.95, "model_tier:strong": 0.95}

    monkeypatch.setattr("kun.interface.llm.router._load_route_credit_scores", fake_load_scores)
    monkeypatch.setenv("KUN_LLM_CREDIT_ROUTING_ENABLED", "1")

    response = await router.invoke(
        LLMRequest(messages=[LLMMessage(role="user", content="tiny")]),
        purpose="execution",
    )

    assert response.model == "model-strong"
    assert response.tier == "strong"
    assert response.route_debug["credit_override"] is True
    assert response.route_debug["credit_to_tier"] == "strong"
    assert response.route_debug["final_planned_tier"] == "strong"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_credit_does_not_downgrade_high_risk(monkeypatch):
    """高风险任务可以被经验升档, 但不能被经验降档。"""

    from kun.engineering.credit_assignment import get_contribution_tracker

    get_contribution_tracker().reset()
    providers = {
        "top": StubProvider(model_id="model-top", tier="top"),
        "strong": StubProvider(model_id="model-strong", tier="strong"),
        "cheap": StubProvider(model_id="model-cheap", tier="cheap"),
        "fallback": StubProvider(model_id="model-fallback", tier="fallback"),
    }
    router = LLMRouter(providers)

    async def fake_load_scores(_resource_keys: list[str]) -> dict[str, float]:
        return {"model_tier:cheap": 1.0}

    monkeypatch.setattr("kun.interface.llm.router._load_route_credit_scores", fake_load_scores)
    monkeypatch.setenv("KUN_LLM_CREDIT_ROUTING_ENABLED", "1")

    response = await router.invoke(
        LLMRequest(
            messages=[LLMMessage(role="user", content="x" * 500)],
            profile=TaskProfile(risk_level="high"),
        ),
        purpose="execution",
    )

    assert response.tier == "top"


@pytest.mark.unit
def test_get_router_can_force_codex_as_primary(monkeypatch):
    """Claude 挂了时可以显式把 Codex MCP 设成主力档位."""
    from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
    from kun.interface.llm.codex_cli_provider import CodexCliProvider
    from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

    reset_router()
    monkeypatch.setenv("KUN_LLM_PRIMARY", "codex")
    monkeypatch.setenv("KUN_DISABLE_CLAUDE_CLI", "1")
    monkeypatch.setenv("KUN_CODEX_MCP_MODEL", "gpt-5.5")
    monkeypatch.delenv("KUN_DISABLE_CLI_OAUTH", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CODEX_CLI", raising=False)
    monkeypatch.delenv("KUN_OFOX_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(ClaudeCodeProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexMcpProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexCliProvider, "available", staticmethod(lambda: False))

    try:
        router = get_router()
        assert router.providers["top"].name == "codex-mcp"
        assert router.providers["top"].model_id == "gpt-5.5"
        assert router.providers["strong"].name == "codex-mcp"
        assert router.providers["cheap"].name == "codex-mcp"
        assert router.providers["coding"].name == "codex-mcp"
    finally:
        reset_router()


@pytest.mark.unit
def test_get_router_can_disable_only_claude_cli(monkeypatch):
    """默认主链路已经是 Codex; 只关 Claude CLI 不会把主链路打回 MiniMax."""
    from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
    from kun.interface.llm.codex_cli_provider import CodexCliProvider
    from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

    reset_router()
    monkeypatch.setenv("KUN_DISABLE_CLAUDE_CLI", "1")
    monkeypatch.setenv("MINIMAX_API_KEY", "dummy")
    monkeypatch.delenv("KUN_LLM_PRIMARY", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CLI_OAUTH", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CODEX_CLI", raising=False)
    monkeypatch.delenv("KUN_OFOX_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(ClaudeCodeProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexMcpProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexCliProvider, "available", staticmethod(lambda: False))

    try:
        router = get_router()
        assert router.providers["top"].name == "codex-mcp"
        assert router.providers["top"].model_id == "gpt-5.5"
        assert router.providers["coding"].name == "codex-mcp"
    finally:
        reset_router()


@pytest.mark.unit
def test_codex_primary_does_not_fallback_to_claude_unless_allowed(monkeypatch):
    """KUN_LLM_PRIMARY=codex 时 Claude 不再误抢主链路."""
    from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
    from kun.interface.llm.codex_cli_provider import CodexCliProvider
    from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

    reset_router()
    monkeypatch.setenv("KUN_LLM_PRIMARY", "codex")
    monkeypatch.delenv("KUN_ALLOW_CLAUDE_FALLBACK", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CLI_OAUTH", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CLAUDE_CLI", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CODEX_CLI", raising=False)
    monkeypatch.delenv("KUN_OFOX_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(ClaudeCodeProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexMcpProvider, "available", staticmethod(lambda: False))
    monkeypatch.setattr(CodexCliProvider, "available", staticmethod(lambda: False))

    try:
        router = get_router()
        assert router.providers["top"].name == "stub"
        assert router.providers["coding"].name == "stub"
    finally:
        reset_router()


@pytest.mark.unit
def test_codex_primary_can_opt_into_claude_fallback(monkeypatch):
    """需要 Claude 兜底时必须显式打开 KUN_ALLOW_CLAUDE_FALLBACK."""
    from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
    from kun.interface.llm.codex_cli_provider import CodexCliProvider
    from kun.interface.llm.codex_mcp_provider import CodexMcpProvider

    reset_router()
    monkeypatch.setenv("KUN_LLM_PRIMARY", "codex")
    monkeypatch.setenv("KUN_ALLOW_CLAUDE_FALLBACK", "1")
    monkeypatch.delenv("KUN_DISABLE_CLI_OAUTH", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CLAUDE_CLI", raising=False)
    monkeypatch.delenv("KUN_DISABLE_CODEX_CLI", raising=False)
    monkeypatch.delenv("KUN_OFOX_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(ClaudeCodeProvider, "available", staticmethod(lambda: True))
    monkeypatch.setattr(CodexMcpProvider, "available", staticmethod(lambda: False))
    monkeypatch.setattr(CodexCliProvider, "available", staticmethod(lambda: False))

    try:
        router = get_router()
        assert router.providers["top"].name == "claude-code-cli"
        assert router.providers["coding"].name == "claude-code-cli"
    finally:
        reset_router()
