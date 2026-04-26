"""C24-a anchor-expand adapters for decision-facing modules."""

from __future__ import annotations

import pytest
from kun.core.strategy_matcher import (
    SignalBundle,
    StrategyCandidate,
    StrategyMatcher,
)
from kun.interface.llm.capability_router import CapabilityRouter, CapabilityScore
from kun.interface.llm.strategy_router_bridge import (
    enumerate_model_candidates_anchor_then_expand,
)
from kun.security.diagnose_runner import DiagnoseFinding, DiagnoseRunner


async def _collect(async_iter) -> list:
    items = []
    async for item in async_iter:
        items.append(item)
    return items


@pytest.mark.asyncio
async def test_strategy_matcher_anchor_yields_top_scored_candidate_first() -> None:
    matcher = StrategyMatcher()

    async def enum(_signals, _prev):
        return [
            StrategyCandidate(
                candidate_id="slow-good",
                description="slow good",
                expected_outcome=0.9,
                expected_cost_usd=0.0,
                expected_latency_sec=30.0,
            ),
            StrategyCandidate(
                candidate_id="fast-good",
                description="fast good",
                expected_outcome=0.8,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            ),
        ]

    matcher.register("model_select", enum)
    items = await _collect(
        matcher.iter_candidates_anchor_then_expand(
            "model_select",
            SignalBundle(task={"risk_level": "low"}),
            max_rounds=1,
        )
    )

    assert [item.candidate.candidate_id for item in items] == ["fast-good"]


@pytest.mark.asyncio
async def test_strategy_matcher_anchor_expands_in_score_order() -> None:
    matcher = StrategyMatcher()

    async def enum(_signals, _prev):
        return [
            StrategyCandidate(
                candidate_id="third",
                description="third",
                expected_outcome=0.5,
                expected_cost_usd=0.2,
                expected_latency_sec=10.0,
            ),
            StrategyCandidate(
                candidate_id="first",
                description="first",
                expected_outcome=0.95,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            ),
            StrategyCandidate(
                candidate_id="second",
                description="second",
                expected_outcome=0.8,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            ),
        ]

    matcher.register("model_select", enum)
    items = await _collect(
        matcher.iter_candidates_anchor_then_expand(
            "model_select",
            SignalBundle(task={"risk_level": "critical"}),
            max_rounds=2,
        )
    )

    assert [item.candidate.candidate_id for item in items] == ["first", "second"]


@pytest.mark.asyncio
async def test_strategy_matcher_anchor_rejects_empty_candidates() -> None:
    matcher = StrategyMatcher()

    async def enum(_signals, _prev):
        return []

    matcher.register("model_select", enum)

    with pytest.raises(RuntimeError, match="empty"):
        await _collect(matcher.iter_candidates_anchor_then_expand("model_select", SignalBundle()))


@pytest.mark.asyncio
async def test_capability_router_anchor_then_expand_limits_rounds() -> None:
    router = CapabilityRouter()

    async def score_for(*, tenant_id: str, model_id: str, task_type: str) -> CapabilityScore:
        scores = {"m1": 0.9, "m2": 0.7, "m3": 0.8}
        return CapabilityScore(
            model_id=model_id,
            task_type=task_type,
            reliability=scores[model_id],
            sample_size=20,
            score=scores[model_id],
            is_cold_start=False,
        )

    router.score_for = score_for  # type: ignore[method-assign]

    items = await _collect(
        router.rank_candidates_anchor_then_expand(
            tenant_id="t1",
            model_ids=["m1", "m2", "m3"],
            task_type="coding",
            max_rounds=2,
        )
    )

    assert [item.model_id for item in items] == ["m1", "m3"]


@pytest.mark.asyncio
async def test_capability_router_anchor_empty_model_list_yields_nothing() -> None:
    router = CapabilityRouter()
    items = await _collect(
        router.rank_candidates_anchor_then_expand(
            tenant_id="t1",
            model_ids=[],
            task_type="coding",
        )
    )

    assert items == []


@pytest.mark.asyncio
async def test_strategy_router_bridge_anchor_then_expand_uses_score_order() -> None:
    items = await _collect(
        enumerate_model_candidates_anchor_then_expand(
            SignalBundle(task={"risk_level": "critical"}),
            max_rounds=2,
        )
    )

    assert [item.metadata["tier"] for item in items] == ["top", "coding"]


@pytest.mark.asyncio
async def test_strategy_router_bridge_anchor_respects_max_rounds() -> None:
    items = await _collect(
        enumerate_model_candidates_anchor_then_expand(
            SignalBundle(task={"risk_level": "low"}),
            max_rounds=3,
        )
    )

    assert len(items) == 3
    assert len({item.candidate_id for item in items}) == 3


def _finding(
    finding_id: str,
    *,
    severity: str = "warn",
    category: str = "clean",
) -> DiagnoseFinding:
    return DiagnoseFinding(
        finding_id=finding_id,
        subsystem="engineering",
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        description=f"{finding_id} description",
    )


@pytest.mark.asyncio
async def test_fix_plan_anchor_prioritizes_severity_then_auto_fix() -> None:
    runner = DiagnoseRunner()
    findings = [
        _finding("low-auto", severity="warn", category="clean"),
        _finding("critical-manual", severity="critical", category="software_mgmt"),
        _finding("critical-auto", severity="critical", category="privacy"),
    ]

    plans = await _collect(runner.generate_fix_plans_anchor_then_expand(findings, max_rounds=2))

    assert [p.target_finding_id for p in plans] == ["critical-auto", "critical-manual"]
    assert plans[0].fix_kind == "auto"
    assert plans[1].fix_kind == "user_confirm_required"


@pytest.mark.asyncio
async def test_fix_plan_anchor_respects_max_rounds() -> None:
    runner = DiagnoseRunner()
    findings = [
        _finding("a", severity="critical", category="privacy"),
        _finding("b", severity="error", category="clean"),
        _finding("c", severity="warn", category="software_mgmt"),
    ]

    plans = await _collect(runner.generate_fix_plans_anchor_then_expand(findings, max_rounds=1))

    assert len(plans) == 1
    assert plans[0].target_finding_id == "a"


@pytest.mark.asyncio
async def test_fix_plan_anchor_creates_pending_confirm_only_for_yielded_manual_plan() -> None:
    runner = DiagnoseRunner()
    findings = [
        _finding("manual", severity="critical", category="software_mgmt"),
        _finding("later", severity="warn", category="admin_policy"),
    ]

    plans = await _collect(runner.generate_fix_plans_anchor_then_expand(findings, max_rounds=1))

    assert len(plans) == 1
    assert plans[0].confirm_token is not None
    assert runner.confirm_user_fix(plans[0].confirm_token) is True
    assert runner.confirm_user_fix(plans[0].confirm_token) is False
