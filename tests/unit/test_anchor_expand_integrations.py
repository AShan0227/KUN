"""Anchor-expand 4 个核心模块集成测试 (V2.2 §19.3 Core 4).

测试 ImportanceScorer / SkillSelector / ContextPacker / multi_judge 都正确
接 anchor-expand 模式.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import ImportanceScorer
from kun.core.anchor_expand import collect_all
from kun.skills.selector import SkillSelector

# ---- ImportanceScorer.score_anchor_then_expand ----


def _make_asset(asset_id: str, kind: str = "memory", text: str = "", tags: list | None = None):
    return LayeredAsset(
        asset_id=asset_id,
        asset_kind=kind,
        l1_metadata={"name": asset_id, "asset_id": asset_id},
        l2_summary=text,
        l3_ref=None,
        tags=tags or [],
        access_count=1,
        last_accessed=datetime.now(UTC),
        tenant_id="t-test",
    )


@pytest.mark.asyncio
async def test_importance_anchor_expand_yields_in_score_order() -> None:
    scorer = ImportanceScorer()
    assets = [
        _make_asset("low", text="totally unrelated"),
        _make_asset("high", text="auth login service architecture", tags=["auth", "login"]),
        _make_asset("mid", text="some auth notes", tags=["auth"]),
    ]
    iterator = scorer.score_anchor_then_expand(
        assets,
        query="auth login",
        max_rounds=3,
        use_marginal_stop=False,
    )
    results = await collect_all(iterator)
    assert len(results) >= 1
    # 第一个 yield 的应该是 score 最高的 (high 包含 query terms)
    first_asset, _first_score = results[0]
    assert first_asset.asset_id == "high"


@pytest.mark.asyncio
async def test_importance_anchor_expand_caller_break() -> None:
    """调用方拿到 anchor 后 break, 不消费后续."""
    scorer = ImportanceScorer()
    assets = [_make_asset(f"a{i}", text=f"mem {i}") for i in range(5)]
    iterator = scorer.score_anchor_then_expand(
        assets,
        query="mem",
        max_rounds=3,
    )
    collected = []
    async for asset, score in iterator:
        collected.append(asset.asset_id)
        if len(collected) >= 1:
            break
    assert len(collected) == 1


@pytest.mark.asyncio
async def test_importance_anchor_expand_empty_candidates() -> None:
    """空 candidates → iterator 立即结束."""
    scorer = ImportanceScorer()
    iterator = scorer.score_anchor_then_expand([], query="x", max_rounds=3)
    results = await collect_all(iterator)
    assert results == []


# ---- SkillSelector.select_anchor_then_expand ----


@pytest.mark.asyncio
async def test_skill_selector_anchor_expand() -> None:
    """SkillSelector 流式选 skill."""
    from kun.datamodel.task import Owner, TaskMeta, TaskRef

    task_ref = TaskRef(
        meta=TaskMeta(
            task_id="tk-1",
            owner=Owner(tenant_id="t-test"),
            fingerprint="sha256:" + "0" * 64,
            task_type="search.web.query",
            risk_level="low",
            complexity_score=0.3,
            estimated_cost_usd=0.01,
            estimated_duration_sec=5.0,
            success_criteria_short="search for info",
        )
    )
    selector = SkillSelector()
    iterator = selector.select_anchor_then_expand(task_ref, max_rounds=3)
    results = await collect_all(iterator)
    # 没注册的 skill 也不应该 crash; 结果可能为空
    assert isinstance(results, list)


# ---- ContextPacker.pack_anchor_then_expand ----


@pytest.mark.asyncio
async def test_context_packer_anchor_expand() -> None:
    """ContextPacker 流式 pack."""
    from kun.context.packer import ContextPacker
    from kun.datamodel.task import Owner, TaskMeta, TaskRef

    task_ref = TaskRef(
        meta=TaskMeta(
            task_id="tk-1",
            owner=Owner(tenant_id="t-test"),
            fingerprint="sha256:" + "0" * 64,
            task_type="x",
            risk_level="low",
            complexity_score=0.3,
            estimated_cost_usd=0.01,
            estimated_duration_sec=5.0,
            success_criteria_short="some task",
        )
    )

    class FakeStore:
        async def list(self, *, tenant_id, asset_kind, limit):
            return []

    packer = ContextPacker(store=FakeStore())
    iterator = packer.pack_anchor_then_expand(task_ref, tenant_id="t-test", max_rounds=3)
    results = await collect_all(iterator)
    assert results == []  # FakeStore 返空


# ---- multi_judge.jury_evaluate_anchor_then_expand ----


@pytest.mark.asyncio
async def test_jury_anchor_expand_returns_valid_verdict() -> None:
    """jury_evaluate_anchor_then_expand 用 stub router 跑通."""
    from kun.engineering.multi_judge import (
        JuryVerdict,
        jury_evaluate_anchor_then_expand,
    )
    from kun.interface.llm import LLMResponse
    from kun.interface.llm.base import UsageInfo

    class StubRouter:
        async def invoke(self, req, **kw):
            return LLMResponse(
                content='{"pass": true, "score": 0.85, "reason": "good"}',
                model="stub",
                provider="stub",
                tier="cheap",
                usage=UsageInfo(input_tokens=10, output_tokens=5),
                cost_usd_actual=0.001,
                cost_usd_equivalent=0.001,
                latency_ms=50.0,
            )

    verdict = await jury_evaluate_anchor_then_expand(
        artifact="some output",
        rubric="check quality",
        judge_models=["judge1", "judge2", "judge3", "judge4", "judge5"],
        router=StubRouter(),  # type: ignore[arg-type]
        max_rounds=5,
        use_marginal_stop=True,
    )
    assert isinstance(verdict, JuryVerdict)
    # 5 个全 pass → consensus 高, marginal_stop 应该早就停 (consensus 100% 不变)
    # 至少 2 个 ballot
    assert len(verdict.ballots) >= 2
    # 全部 pass → majority True
    assert verdict.pass_ is True


@pytest.mark.asyncio
async def test_jury_anchor_expand_too_few_judges_inconclusive() -> None:
    from kun.engineering.multi_judge import jury_evaluate_anchor_then_expand
    from kun.interface.llm import LLMResponse
    from kun.interface.llm.base import UsageInfo

    class StubRouter:
        async def invoke(self, req, **kw):
            return LLMResponse(
                content='{"pass": true, "score": 0.7, "reason": "ok"}',
                model="stub",
                provider="stub",
                tier="cheap",
                usage=UsageInfo(input_tokens=10, output_tokens=5),
                cost_usd_actual=0.001,
                cost_usd_equivalent=0.001,
                latency_ms=50.0,
            )

    verdict = await jury_evaluate_anchor_then_expand(
        artifact="x",
        rubric="x",
        judge_models=["judge1"],  # 只 1 个
        router=StubRouter(),  # type: ignore[arg-type]
        max_rounds=5,
    )
    # 只 1 个 ballot → inconclusive
    assert verdict.pass_ is False
    assert "inconclusive" in verdict.rationale
