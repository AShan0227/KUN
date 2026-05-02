"""Anchor-Then-Expand iterator 单测 (V2.2 §19.3)."""

from __future__ import annotations

import pytest
from kun.core.anchor_expand import (
    AnchorExpandIterator,
    ExpansionStats,
    collect_all,
    collect_until,
)
from kun.engineering.marginal_roi import (
    MarginalROIStopCriterion,
    ValueEstimator,
)

# ---- 基础流式 ----


@pytest.mark.asyncio
async def test_anchor_only_max_rounds_1() -> None:
    """max_rounds=1 → 只 yield anchor."""

    async def anchor():
        return "A"

    async def expand(a, prior):
        raise AssertionError("should not be called")

    items = await collect_all(AnchorExpandIterator(anchor, expand, max_rounds=1))
    assert items == ["A"]


@pytest.mark.asyncio
async def test_yields_anchor_then_expansions() -> None:
    """max_rounds=3 → anchor + 2 expand."""
    counter = [0]

    async def anchor():
        return "anchor"

    async def expand(a, prior):
        counter[0] += 1
        return f"exp_{counter[0]}"

    items = await collect_all(AnchorExpandIterator(anchor, expand, max_rounds=3))
    assert items == ["anchor", "exp_1", "exp_2"]


@pytest.mark.asyncio
async def test_expand_returns_none_stops() -> None:
    """expand 返 None → 提前结束."""

    async def anchor():
        return "A"

    async def expand(a, prior):
        if len(prior) >= 2:
            return None
        return f"exp_{len(prior)}"

    iterator = AnchorExpandIterator(anchor, expand, max_rounds=5)
    items = await collect_all(iterator)
    assert items == ["A", "exp_1"]
    assert iterator.stats.stopped_reason == "expand_returned_none"
    assert iterator.stats.rounds_completed == 2


@pytest.mark.asyncio
async def test_caller_break_after_anchor() -> None:
    """调用方 break, expand 不被调."""
    expand_called = [0]

    async def anchor():
        return "A"

    async def expand(a, prior):
        expand_called[0] += 1
        return "exp"

    iterator = AnchorExpandIterator(anchor, expand, max_rounds=3)
    async for item in iterator:
        assert item == "A"
        break
    assert expand_called[0] == 0


# ---- max_rounds 校验 ----


def test_max_rounds_must_be_in_range() -> None:
    async def anchor():
        return "A"

    async def expand(a, prior):
        return "B"

    with pytest.raises(ValueError):
        AnchorExpandIterator(anchor, expand, max_rounds=0)
    with pytest.raises(ValueError):
        AnchorExpandIterator(anchor, expand, max_rounds=11)


# ---- 异常容错 ----


@pytest.mark.asyncio
async def test_anchor_exception_yields_nothing() -> None:
    async def anchor_fail():
        raise ValueError("anchor crash")

    async def expand(a, prior):
        return "B"

    iterator = AnchorExpandIterator(anchor_fail, expand)
    items = await collect_all(iterator)
    assert items == []
    assert "anchor_exception" in iterator.stats.stopped_reason


@pytest.mark.asyncio
async def test_expand_exception_stops_at_anchor() -> None:
    async def anchor():
        return "A"

    async def expand_fail(a, prior):
        raise RuntimeError("expand crash")

    iterator = AnchorExpandIterator(anchor, expand_fail, max_rounds=3)
    items = await collect_all(iterator)
    assert items == ["A"]
    assert "expand_exception" in iterator.stats.stopped_reason


# ---- marginal stop 集成 ----


@pytest.mark.asyncio
async def test_marginal_stop_integration_stops_early() -> None:
    """anchor value=0.8, expand value=0.81 (marginal +0.01 < threshold 0.05) → 停."""

    async def anchor():
        return {"name": "A", "quality": 0.8}

    async def expand(a, prior):
        idx = len(prior)
        return {"name": f"E{idx}", "quality": 0.8 + idx * 0.005}  # 每步 +0.005, 极慢

    criterion = MarginalROIStopCriterion(delta_threshold=0.05, window_k=1, min_steps=2)
    estimator = ValueEstimator(strategy="cumulative_quality")

    iterator = AnchorExpandIterator(
        anchor,
        expand,
        max_rounds=5,
        stop_criterion=criterion,
        value_estimator=estimator,
    )
    items = await collect_all(iterator)
    # 跑完 anchor + 1 expand 就该停 (min_steps=2 满足, marginal +0.005 < 0.05)
    assert len(items) == 2
    assert iterator.stats.stopped_reason == "marginal_stop"


@pytest.mark.asyncio
async def test_marginal_no_stop_when_improving_fast() -> None:
    """每步带来大提升 → 不停, 跑满 max_rounds."""

    async def anchor():
        return {"quality": 0.3}

    async def expand(a, prior):
        return {"quality": 0.3 + len(prior) * 0.2}  # 每步 +0.2 大幅提升

    criterion = MarginalROIStopCriterion(delta_threshold=0.05, window_k=1, min_steps=2)
    estimator = ValueEstimator(strategy="cumulative_quality")

    iterator = AnchorExpandIterator(
        anchor,
        expand,
        max_rounds=3,
        stop_criterion=criterion,
        value_estimator=estimator,
    )
    items = await collect_all(iterator)
    assert len(items) == 3
    assert iterator.stats.stopped_reason == "max_rounds"


def test_stop_criterion_requires_estimator() -> None:
    async def anchor():
        return "A"

    async def expand(a, prior):
        return "B"

    criterion = MarginalROIStopCriterion()
    with pytest.raises(ValueError):
        AnchorExpandIterator(
            anchor,
            expand,
            stop_criterion=criterion,
            value_estimator=None,
        )


# ---- on_done callback ----


@pytest.mark.asyncio
async def test_on_done_callback_fires_with_stats() -> None:
    captured: list[ExpansionStats] = []

    async def anchor():
        return "A"

    async def expand(a, prior):
        return f"e{len(prior)}"

    iterator = AnchorExpandIterator(
        anchor,
        expand,
        max_rounds=3,
        on_done=lambda stats: captured.append(stats),
    )
    await collect_all(iterator)
    assert len(captured) == 1
    assert captured[0].rounds_completed == 3
    assert captured[0].items_yielded == 3
    assert captured[0].stopped_reason == "max_rounds"


@pytest.mark.asyncio
async def test_on_done_callback_exception_doesnt_break() -> None:
    """on_done 抛异常不影响主流程."""

    def bad_callback(stats):
        raise ValueError("callback boom")

    async def anchor():
        return "A"

    async def expand(a, prior):
        return None  # 立即停

    iterator = AnchorExpandIterator(
        anchor,
        expand,
        max_rounds=3,
        on_done=bad_callback,
    )
    items = await collect_all(iterator)
    # 主流程仍正常
    assert items == ["A"]


# ---- collect_until helper ----


@pytest.mark.asyncio
async def test_collect_until_predicate() -> None:
    """收集直到 len ≥ 2 就停."""

    async def anchor():
        return "A"

    async def expand(a, prior):
        return f"e{len(prior)}"

    items = await collect_until(
        AnchorExpandIterator(anchor, expand, max_rounds=5),
        predicate=lambda items: len(items) >= 2,
    )
    assert items == ["A", "e1"]


# ---- 实际场景模拟 ----


@pytest.mark.asyncio
async def test_realistic_memory_scenario() -> None:
    """模拟拉记忆: 第一条够用直接 break, 不调 expand."""

    memories = [
        {"id": "mem_1", "content": "auth_service 架构图", "quality": 0.9},
        {"id": "mem_2", "content": "JWT 用法", "quality": 0.8},
        {"id": "mem_3", "content": "RLS 策略", "quality": 0.7},
    ]

    async def anchor():
        return memories[0]

    async def expand(a, prior):
        idx = len(prior)
        if idx >= len(memories):
            return None
        return memories[idx]

    iterator = AnchorExpandIterator(anchor, expand, max_rounds=3)

    # 模拟 LLM 拉到第一条说"够用"
    collected = []
    async for mem in iterator:
        collected.append(mem)
        # 这个场景: 第 1 条 quality=0.9 已经够, break
        break
    assert len(collected) == 1
    assert collected[0]["id"] == "mem_1"


@pytest.mark.asyncio
async def test_realistic_skill_scenario_expand_to_2() -> None:
    """模拟 skill 选择: 第一个不够 (overlap 低), 拉到第 2 个."""

    skills = [
        {"id": "skill_search", "match_score": 0.7},
        {"id": "skill_summarize", "match_score": 0.5},
        {"id": "skill_translate", "match_score": 0.3},
    ]

    async def anchor():
        return skills[0]

    async def expand(a, prior):
        idx = len(prior)
        if idx >= len(skills):
            return None
        return skills[idx]

    iterator = AnchorExpandIterator(anchor, expand, max_rounds=3)
    collected = []
    async for skill in iterator:
        collected.append(skill)
        if len(collected) >= 2:  # 模拟"2 个 skill 够 cover task"
            break
    assert len(collected) == 2
    assert [s["id"] for s in collected] == ["skill_search", "skill_summarize"]
