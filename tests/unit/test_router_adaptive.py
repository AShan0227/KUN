"""Router: complexity heuristic + quota-aware downgrade layers."""

from __future__ import annotations

import pytest
from kun.core.quota_tracker import QuotaTracker, reset_tracker, set_tracker
from kun.interface.llm import LLMMessage, LLMRequest, LLMRouter, TaskProfile
from kun.interface.llm.stub_provider import StubProvider


def _fresh_router() -> LLMRouter:
    return LLMRouter(
        providers={
            "top": StubProvider(model_id="top", tier="top"),
            "strong": StubProvider(model_id="strong", tier="strong"),
            "cheap": StubProvider(model_id="cheap", tier="cheap"),
            "coding": StubProvider(model_id="coding", tier="coding"),
            "fallback": StubProvider(model_id="fb", tier="fallback"),
        }
    )


def _spacious_tracker() -> QuotaTracker:
    return QuotaTracker(limits={"top": 1000, "strong": 1000, "cheap": 10_000, "fallback": 10_000})


@pytest.fixture(autouse=True)
def _tracker_sandbox():
    """Every test gets its own tracker; reset after."""
    set_tracker(_spacious_tracker())
    yield
    reset_tracker()


# ---------- Layer 3: complexity ----------


@pytest.mark.unit
def test_complexity_simple_downgrades_top_to_cheap():
    router = _fresh_router()
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])
    decision = router.decide("intent", request=req)
    # `intent` normally → top; short prompt → cheap
    assert decision.primary_tier == "cheap"
    assert "complexity=simple" in decision.rationale


@pytest.mark.unit
def test_complexity_complex_upgrades_cheap_to_strong():
    router = _fresh_router()
    long = "x" * 4000  # well over the complex threshold
    req = LLMRequest(messages=[LLMMessage(role="user", content=long)])
    decision = router.decide("classification", request=req)
    # `classification` normally → cheap; long prompt → strong
    assert decision.primary_tier == "strong"
    assert "complexity=complex" in decision.rationale


@pytest.mark.unit
def test_complexity_medium_keeps_purpose_tier():
    router = _fresh_router()
    mid = "x" * 1500
    req = LLMRequest(messages=[LLMMessage(role="user", content=mid)])
    decision = router.decide("intent", request=req)  # top
    assert decision.primary_tier == "top"


@pytest.mark.unit
def test_coding_tier_not_touched_by_complexity():
    router = _fresh_router()
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])  # short
    decision = router.decide("coding", request=req)
    assert decision.primary_tier == "coding"  # not downgraded to cheap


# ---------- Layer 4: quota ----------


@pytest.mark.unit
def test_quota_saturation_downgrades_top_to_strong():
    set_tracker(QuotaTracker(limits={"top": 0, "strong": 10, "cheap": 100, "fallback": 1000}))
    router = _fresh_router()
    # Non-simple prompt so complexity doesn't move the target first
    req = LLMRequest(messages=[LLMMessage(role="user", content="x" * 1500)])
    decision = router.decide("execution", request=req)
    assert decision.primary_tier == "strong"
    assert "quota:top→strong" in decision.rationale


@pytest.mark.unit
def test_quota_cascades_through_full_chain():
    set_tracker(QuotaTracker(limits={"top": 0, "strong": 0, "cheap": 0, "fallback": 1000}))
    router = _fresh_router()
    req = LLMRequest(messages=[LLMMessage(role="user", content="x" * 1500)])
    decision = router.decide("execution", request=req)
    assert decision.primary_tier == "fallback"


@pytest.mark.unit
def test_critical_pin_survives_complexity_but_not_quota():
    # Critical pin happens before quota; if top is available, critical wins.
    router = _fresh_router()
    req = LLMRequest(messages=[LLMMessage(role="user", content="hi")])  # simple
    profile = TaskProfile(risk_level="critical")
    decision = router.decide("classification", profile, request=req)
    assert decision.primary_tier == "top"  # critical pinned past complexity

    # But a fully saturated `top` will still downgrade even for critical.
    set_tracker(QuotaTracker(limits={"top": 0, "strong": 10, "cheap": 100, "fallback": 1000}))
    router2 = _fresh_router()
    decision2 = router2.decide("classification", profile, request=req)
    assert decision2.primary_tier == "strong"


# ---------- record() side-effect ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_records_usage_against_tracker():
    from kun.core.quota_tracker import get_tracker

    set_tracker(_spacious_tracker())
    router = _fresh_router()
    req = LLMRequest(messages=[LLMMessage(role="user", content="x" * 1500)])
    before = get_tracker().usage("top")
    await router.invoke(req, purpose="execution")
    after = get_tracker().usage("top")
    assert after == before + 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_path_records_fallback_tier():
    from kun.core.quota_tracker import get_tracker

    set_tracker(_spacious_tracker())
    router = LLMRouter(
        providers={
            "top": StubProvider(model_id="flaky", tier="top", fail_rate=1.0),
            "strong": StubProvider(model_id="strong", tier="strong"),
            "cheap": StubProvider(model_id="cheap", tier="cheap"),
            "fallback": StubProvider(model_id="fb", tier="fallback"),
        }
    )
    req = LLMRequest(messages=[LLMMessage(role="user", content="x" * 1500)])
    before_fb = get_tracker().usage("fallback")
    await router.invoke(req, purpose="execution")
    assert get_tracker().usage("fallback") == before_fb + 1


# ---------- L1.6 · capability-aware tier adjustment (ADR-024 闭环) ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_adjustment_upgrades_low_reliability_tier():
    """L1.6: capability_card 显示当前 tier 的代表模型 score < 0.4 + sample >= 10
    → 自动升级 tier (cheap → strong). 这是"执行→能力卡→路由"真闭环."""
    import time

    from kun.interface.llm.capability_router import (
        CapabilityScore,
        get_capability_router,
        reset_capability_router,
    )

    reset_capability_router()
    cap_router = get_capability_router()

    # Pre-populate cache: cheap 档代表模型 在 classification 任务上有差证据
    cap_router._cache[("u-sylvan", "cheap-model", "classification")] = (
        time.monotonic() + 1000,  # not expired
        CapabilityScore(
            model_id="cheap-model",
            task_type="classification",
            reliability=0.3,
            sample_size=20,
            score=0.3,
            is_cold_start=False,
        ),
    )

    router = LLMRouter(
        providers={
            "top": StubProvider(model_id="top-model", tier="top"),
            "strong": StubProvider(model_id="strong-model", tier="strong"),
            "cheap": StubProvider(model_id="cheap-model", tier="cheap"),
            "fallback": StubProvider(model_id="fb", tier="fallback"),
        }
    )

    response = await router.invoke(
        LLMRequest(
            messages=[LLMMessage(role="user", content="classify this")],
            profile=TaskProfile(task_type="classification"),
        ),
        purpose="classification",  # → 默认 cheap, 但应被升级到 strong
    )

    # 升级后用了 strong (strong-model)
    assert response.tier == "strong", (
        f"expected upgrade to strong, got {response.tier} ({response.model})"
    )
    assert response.model == "strong-model"

    reset_capability_router()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_adjustment_keeps_tier_when_signal_weak():
    """L1.6: cold start (sample < 10) → 不调整 tier, 保留 decide() 的默认结果."""
    import time

    from kun.interface.llm.capability_router import (
        CapabilityScore,
        get_capability_router,
        reset_capability_router,
    )

    reset_capability_router()
    cap_router = get_capability_router()

    # 弱信号: sample 只有 3, 即使 score 低也不应触发升级
    cap_router._cache[("u-sylvan", "cheap-model", "classification")] = (
        time.monotonic() + 1000,
        CapabilityScore(
            model_id="cheap-model",
            task_type="classification",
            reliability=0.2,
            sample_size=3,
            score=0.2,
            is_cold_start=False,
        ),
    )

    router = LLMRouter(
        providers={
            "top": StubProvider(model_id="top-model", tier="top"),
            "strong": StubProvider(model_id="strong-model", tier="strong"),
            "cheap": StubProvider(model_id="cheap-model", tier="cheap"),
            "fallback": StubProvider(model_id="fb", tier="fallback"),
        }
    )

    response = await router.invoke(
        LLMRequest(
            messages=[LLMMessage(role="user", content="quick")],
            profile=TaskProfile(task_type="classification"),
        ),
        purpose="classification",
    )

    # 信号弱 → 不升级, 保持 cheap
    assert response.tier == "cheap"
    assert response.model == "cheap-model"

    reset_capability_router()
