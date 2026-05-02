"""LLM route governance tests."""

from __future__ import annotations

from typing import Any

import pytest
from kun.watchtower.llm_route_governance import (
    CostExceededError,
    LLMRouteGovernor,
    ModelTrustError,
)


class FakeRuleEngine:
    def __init__(self, fired: list[str] | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fired = fired or []

    async def evaluate(self, event_type: str, *, namespace: dict[str, Any]) -> list[str]:
        self.events.append((event_type, namespace))
        return self.fired


class FakeCapabilityRouter:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    async def model_scores(self, task_type: str, candidate_models: list[str]) -> dict[str, float]:
        assert task_type == "coding.python"
        return {model: self.scores.get(model, 0.0) for model in candidate_models}


@pytest.mark.asyncio
async def test_consult_selects_highest_capability_score() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"cheap": 0.4, "top": 0.9, "strong": 0.7}),
    )

    selected = await governor.consult_for_model_select(
        {"task_type": "coding.python", "tenant_id": "t-1"},
        ["cheap", "top", "strong"],
    )

    assert selected == "top"


@pytest.mark.asyncio
async def test_consult_keeps_candidate_order_on_tie() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"cheap": 0.8, "strong": 0.8}),
    )

    selected = await governor.consult_for_model_select(
        {"task_type": "coding.python"},
        ["cheap", "strong"],
    )

    assert selected == "cheap"


@pytest.mark.asyncio
async def test_consult_evaluates_watchtower_event() -> None:
    engine = FakeRuleEngine(fired=["route-observed"])
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"top": 0.9}),
    )

    selected = await governor.consult_for_model_select(
        {"task_type": "coding.python", "tenant_id": "tenant-a"},
        ["top"],
    )

    assert selected == "top"
    assert engine.events[0][0] == "llm.model_select.consulted"
    assert engine.events[0][1]["selected_model"] == "top"
    assert engine.events[0][1]["tenant_id"] == "tenant-a"
    ticket = engine.events[0][1]["decision_ticket"]
    assert ticket["decision_point"] == "llm_model_selected"
    assert ticket["source_module"] == "watchtower.llm_route_governance"
    assert ticket["selected_action"] == "top"
    assert ticket["status"] == "selected"
    assert ticket["task_id"].startswith("route:")


@pytest.mark.asyncio
async def test_consult_redacts_private_task_meta_before_rule_event() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"top": 0.9}),
    )

    selected = await governor.consult_for_model_select(
        {
            "task_type": "coding.python",
            "tenant_id": "tenant-a",
            "prompt": "email sylvan@example.com api_key=sk-secret123456",
        },
        ["top"],
    )

    assert selected == "top"
    emitted = str(engine.events[0][1]["task_meta"])
    assert "sylvan@example.com" not in emitted
    assert "sk-secret123456" not in emitted
    assert "[REDACTED_EMAIL]" in emitted
    assert "[REDACTED_SECRET]" in emitted


@pytest.mark.asyncio
async def test_consult_blocks_when_cost_ceiling_exceeded() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"top": 0.9}),
    )

    with pytest.raises(CostExceededError):
        await governor.consult_for_model_select(
            {
                "task_type": "coding.python",
                "tenant_id": "tenant-a",
                "estimated_cost_usd": 3.5,
                "cost_ceiling_usd": 2.0,
            },
            ["top"],
        )

    assert engine.events[0][0] == "llm.model_select.blocked"
    assert engine.events[0][1]["reason"] == "cost_ceiling"
    ticket = engine.events[0][1]["decision_ticket"]
    assert ticket["status"] == "blocked"
    assert ticket["selected_action"] == "blocked"
    assert ticket["metadata"]["block_reason"] == "cost_ceiling"


@pytest.mark.asyncio
async def test_consult_skips_distrusted_models() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"cheap": 0.2, "top": 0.99, "strong": 0.8}),
    )

    selected = await governor.consult_for_model_select(
        {"task_type": "coding.python", "distrusted_models": ["top"]},
        ["cheap", "top", "strong"],
    )

    assert selected == "strong"
    assert engine.events[0][1]["candidate_models"] == ["cheap", "strong"]
    assert engine.events[0][1]["original_candidate_models"] == ["cheap", "top", "strong"]


@pytest.mark.asyncio
async def test_consult_blocks_when_all_models_are_distrusted() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(
        engine,  # type: ignore[arg-type]
        FakeCapabilityRouter({"top": 0.9}),
    )

    with pytest.raises(ModelTrustError):
        await governor.consult_for_model_select(
            {"task_type": "coding.python", "distrusted_models": ["top"]},
            ["top"],
        )

    assert engine.events[0][0] == "llm.model_select.blocked"
    assert engine.events[0][1]["reason"] == "model_trust"
    ticket = engine.events[0][1]["decision_ticket"]
    assert ticket["status"] == "blocked"
    assert ticket["source_module"] == "watchtower.llm_route_governance"


@pytest.mark.asyncio
async def test_consult_falls_back_to_first_candidate_without_history() -> None:
    engine = FakeRuleEngine()
    governor = LLMRouteGovernor(engine, object())  # type: ignore[arg-type]

    selected = await governor.consult_for_model_select(
        {"task_type": "unknown"},
        ["cheap", "top"],
    )

    assert selected == "cheap"


@pytest.mark.asyncio
async def test_consult_supports_score_model_fallback() -> None:
    class ScoreModelRouter:
        async def score_model(self, task_type: str, model_id: str) -> float:
            assert task_type == "coding.python"
            return {"cheap": 0.2, "strong": 0.6}[model_id]

    governor = LLMRouteGovernor(FakeRuleEngine(), ScoreModelRouter())  # type: ignore[arg-type]

    selected = await governor.consult_for_model_select(
        {"task_type": "coding.python"},
        ["cheap", "strong"],
    )

    assert selected == "strong"


@pytest.mark.asyncio
async def test_consult_rejects_empty_candidates() -> None:
    governor = LLMRouteGovernor(FakeRuleEngine(), object())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await governor.consult_for_model_select({"task_type": "x"}, [])


@pytest.mark.asyncio
async def test_trigger_route_change_creates_shadow_proposal() -> None:
    engine = FakeRuleEngine(fired=["route-change"])
    governor = LLMRouteGovernor(engine, object())  # type: ignore[arg-type]

    await governor.trigger_route_change(
        task_type="coding.python",
        from_model="cheap",
        to_model="strong",
        reason="cheap success rate dropped",
    )

    proposal = governor.route_change_proposals[0]
    assert proposal.phase == "shadow"
    assert proposal.rollout_percent == 0.0
    assert proposal.fired_rules == ["route-change"]
    assert engine.events[0][0] == "llm.route_change.proposed"
    ticket = engine.events[0][1]["decision_ticket"]
    assert ticket["task_id"] == "route-policy:coding.python"
    assert ticket["selected_action"] == "cheap->strong"
    assert ticket["status"] == "needs_review"
