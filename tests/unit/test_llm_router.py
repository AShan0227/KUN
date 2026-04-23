"""Router tier decision + fallback tests."""

import pytest
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMRouter,
    TaskProfile,
)
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
        LLMRequest(messages=[LLMMessage(role="user", content="hi")]),
        purpose="execution",
    )
    assert response.provider == "stub"
    assert response.tier == "fallback"


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
