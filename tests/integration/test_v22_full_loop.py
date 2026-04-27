"""V2.2 full-loop integration smoke test.

This test keeps external services mocked, but wires the real runtime objects:
install_runtime → lab recipe bridge → Hermes generator → orchestrator →
verification runner → graph traversal based mempalace expansion.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from kun.api.runtime import install_runtime
from kun.context.importance import ImportanceScorer
from kun.datamodel.layered_asset import LayeredAsset
from kun.datamodel.verification_spec import VerificationResult, VerificationSpec
from kun.engineering.precipitation import PrecipitationEvent
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.lab import reset_adoption_step, reset_recipe_registry
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger
from starlette.datastructures import State


class _FakeSession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        class R:
            def scalar_one_or_none(self) -> None:
                return None

            def scalar_one(self) -> int:
                return 0

            def all(self) -> list[Any]:
                return []

            def scalars(self) -> Any:
                return self

        return R()

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@asynccontextmanager
async def _fake_session_scope(**_kwargs: Any) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


async def _identity_translator(**kwargs: Any) -> str:
    return str(kwargs["payload"]["answer"])


def _intent_response_with_verification() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "task_type": "writing.simple",
                "risk_level": "low",
                "complexity_score": 0.5,
                "estimated_cost_usd": 0.01,
                "estimated_duration_sec": 5,
                "success_criteria_short": "say hello",
                "goal_detail": "say hello",
                "verification_specs": [
                    {"kind": "exact_output", "spec": {"expected": "hello"}},
                ],
            }
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )


def _hermes_direct_step_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "step_id": 1,
                "thought": "Direct LLM is enough for this simple response.",
                "action_type": "direct_llm",
                "action_payload": {},
                "expected_outcome": "return hello",
                "confidence": 0.8,
                "cost_estimate_usd": 0.01,
            }
        ),
        usage=UsageInfo(input_tokens=20, output_tokens=30),
    )


class _V22RouterStub(StubProvider):
    async def invoke(self, request: Any) -> LLMResponse:
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            return _intent_response_with_verification()
        if "Hermes" in sys_text and "structured execution planner" in sys_text:
            return _hermes_direct_step_response()
        return LLMResponse(content="hello", usage=UsageInfo(input_tokens=10, output_tokens=4))


def _install_router() -> None:
    stub = _V22RouterStub(tier="top")
    set_router(
        LLMRouter(
            {
                "top": stub,
                "strong": stub,
                "cheap": stub,
                "coding": stub,
                "fallback": stub,
            }
        )
    )


class _RecordingVerificationRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[VerificationSpec, str]] = []

    async def verify(self, spec: VerificationSpec, artifact: str) -> VerificationResult:
        self.calls.append((spec, artifact))
        return VerificationResult(kind=spec.kind, passed=True)


def _empty_rule_engine() -> RuleEngine:
    return RuleEngine(
        [
            GuardRule(
                id="noop",
                kind="guard",
                trigger=RuleTrigger(event_type="*", when="True"),
            )
        ]
    )


@pytest.mark.asyncio
async def test_v22_runtime_orchestrator_lab_verification_and_mempalace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_adoption_step()
    reset_recipe_registry()
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    _install_router()

    with patch.dict(
        os.environ,
        {
            "KUN_LAB_BRIDGE_ENABLED": "1",
            "KUN_HERMES_ENABLED": "1",
            "KUN_VERIFICATION_ENABLED": "1",
            "KUN_VALUE_GATE_ENABLED": "0",
        },
    ):
        app = SimpleNamespace(state=State())
        install_runtime(app, rule_engine=_empty_rule_engine())

    verification = _RecordingVerificationRunner()
    app.state.verification_runner = verification
    app.state.orchestrator.verification_runner = verification
    app.state.orchestrator.output_translator = _identity_translator

    # Lab recipe bridge: experiment.promoted → KP → LabRecipeRegistry.
    updates = await app.state.knowledge_precipitation.dispatch(
        PrecipitationEvent(
            event_id="prom-v22-full-loop",
            event_type="experiment.promoted",
            payload={
                "promotion_id": "prom-v22-full-loop",
                "task_type": "writing.simple",
                "strategy": "tier_top_low_temp",
                "win_rate": 0.88,
                "total_count": 12,
                "target_module": "execution_mode_classifier",
            },
        )
    )
    assert len(updates) == 1
    recipe = app.state.lab_recipe_registry.get("writing.simple", "execution_mode_classifier")
    assert recipe is not None
    assert recipe.strategy == "tier_top_low_temp"

    events: list[tuple[str, dict[str, Any]]] = []
    async for event in app.state.orchestrator.stream("say hello"):
        events.append((event.kind, event.data))

    kinds = [kind for kind, _data in events]
    assert "hermes_step" in kinds
    assert "verification_done" in kinds
    assert "done" in kinds
    assert verification.calls
    assert verification.calls[0][0].kind == "exact_output"
    assert verification.calls[0][1] == "hello"

    # Mempalace path: anchor asset expands through graph neighbor before a high
    # scoring but unrelated candidate.
    anchor = LayeredAsset(
        asset_id="asset-anchor",
        asset_kind="memory",
        tenant_id="u-sylvan",
        l1_metadata={"entity_id": "asset-anchor", "importance_signal": 0.95},
    )
    neighbor = LayeredAsset(
        asset_id="asset-neighbor",
        asset_kind="memory",
        tenant_id="u-sylvan",
        l1_metadata={"entity_id": "asset-neighbor", "importance_signal": 0.2},
    )
    unrelated = LayeredAsset(
        asset_id="asset-unrelated",
        asset_kind="memory",
        tenant_id="u-sylvan",
        l1_metadata={"entity_id": "asset-unrelated", "importance_signal": 0.8},
    )
    traversal = AsyncMock()
    traversal.neighbors = AsyncMock(
        return_value=[
            SimpleNamespace(
                entity_kind="asset",
                entity_id="asset-neighbor",
                relation_type="depends_on",
                confidence=0.9,
                hops=1,
                score=0.9,
            )
        ]
    )
    iterator = ImportanceScorer().score_anchor_then_expand(
        [anchor, unrelated, neighbor],
        query="hello",
        graph_traversal=traversal,
        candidate_entity_kind="asset",
        max_rounds=2,
        use_marginal_stop=False,
    )
    ordered: list[LayeredAsset] = []
    async for asset, _score in iterator:
        ordered.append(asset)
    assert [asset.asset_id for asset in ordered[:2]] == ["asset-anchor", "asset-neighbor"]

    reset_adoption_step()
    reset_recipe_registry()
