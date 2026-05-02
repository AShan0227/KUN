"""TaskBoundaryGuard 单测 (V2.2 §28 / Wire 18, OffTopicEval 启发)."""

from __future__ import annotations

import pytest
from kun.security.task_boundary_guard import (
    BoundaryDecision,
    ScopeConfig,
    TaskBoundaryGuard,
)

# ---- 没 scope ----


@pytest.mark.asyncio
async def test_no_scope_returns_neutral_in_scope() -> None:
    guard = TaskBoundaryGuard()
    d = await guard.check({"task_type": "anything"}, scope=None)
    assert d.in_scope is True
    assert d.boundary_score == 0.7
    assert d.reason == "no_scope_defined"


@pytest.mark.asyncio
async def test_empty_scope_lists_treated_as_no_scope() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(role_id="x", role_name="any")
    d = await guard.check({"task_type": "x"}, scope=scope)
    assert d.reason == "no_scope_defined"


# ---- whitelist ----


@pytest.mark.asyncio
async def test_whitelist_exact_match() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="marketing",
        allowed_task_types=["marketing.copywriting"],
    )
    d = await guard.check({"task_type": "marketing.copywriting"}, scope=scope)
    assert d.in_scope is True
    assert d.boundary_score == 1.0
    assert d.reason == "whitelist_match"
    assert d.matched_pattern == "marketing.copywriting"


@pytest.mark.asyncio
async def test_whitelist_wildcard_prefix() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="marketing",
        allowed_task_types=["marketing.*"],
    )
    d = await guard.check({"task_type": "marketing.ad.video"}, scope=scope)
    assert d.in_scope is True
    assert d.matched_pattern == "marketing.*"


# ---- blacklist ----


@pytest.mark.asyncio
async def test_blacklist_hit_rejects_even_in_whitelist() -> None:
    """黑名单优先级高于白名单."""
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="marketing",
        allowed_task_types=["*"],  # 这里 wildcard 本身没用 (后缀 *)
        forbidden_task_types=["coding.*"],
        out_of_scope_redirect="coding-agent",
    )
    d = await guard.check({"task_type": "coding.python.fastapi"}, scope=scope)
    assert d.in_scope is False
    assert d.boundary_score == 0.0
    assert d.reason == "blacklist_hit"
    assert d.suggested_redirect == "coding-agent"


# ---- LLM judge ----


@pytest.mark.asyncio
async def test_llm_judge_high_score_in_scope() -> None:
    async def fake_judge(task_meta, scope):
        return 0.85

    guard = TaskBoundaryGuard(llm_judge=fake_judge, threshold=0.4)
    scope = ScopeConfig(
        role_id="marketing",
        role_name="marketing agent",
        allowed_task_types=["marketing.copywriting"],  # 不会命中 task_type
    )
    d = await guard.check({"task_type": "creative.brainstorm"}, scope=scope)
    assert d.in_scope is True
    assert d.reason == "llm_judge"
    assert d.boundary_score == 0.85


@pytest.mark.asyncio
async def test_llm_judge_low_score_rejected() -> None:
    async def fake_judge(task_meta, scope):
        return 0.2

    guard = TaskBoundaryGuard(llm_judge=fake_judge, threshold=0.4)
    scope = ScopeConfig(
        role_id="marketing",
        role_name="marketing",
        allowed_task_types=["marketing.copywriting"],
        out_of_scope_redirect="general-agent",
    )
    d = await guard.check({"task_type": "math.algebra"}, scope=scope)
    assert d.in_scope is False
    assert d.suggested_redirect == "general-agent"


@pytest.mark.asyncio
async def test_llm_judge_exception_falls_back_to_heuristic() -> None:
    async def bad_judge(task_meta, scope):
        raise RuntimeError("llm down")

    guard = TaskBoundaryGuard(llm_judge=bad_judge)
    scope = ScopeConfig(
        role_id="x",
        role_name="marketing copywriter",
        allowed_task_types=["marketing.copywriting"],
    )
    # 退化启发式
    d = await guard.check({"task_type": "marketing.ad"}, scope=scope)
    # 启发式 (marketing 词重叠) → 0.8 → in_scope
    assert d.reason == "heuristic_overlap"
    assert d.in_scope is True


# ---- 启发式 ----


@pytest.mark.asyncio
async def test_heuristic_word_overlap_high() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="x",
        role_name="python coding agent",
        allowed_task_types=["other.*"],  # 不命中
    )
    d = await guard.check({"task_type": "coding.python.fastapi"}, scope=scope)
    assert d.in_scope is True  # python + coding 重叠
    assert d.reason == "heuristic_overlap"


@pytest.mark.asyncio
async def test_heuristic_no_overlap_rejected() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="x",
        role_name="marketing",
        allowed_task_types=["marketing.copywriting"],
    )
    # task_type math.* vs role marketing → 词不重叠 → 0.2 → reject
    d = await guard.check({"task_type": "math.algebra.linear"}, scope=scope)
    assert d.in_scope is False


# ---- stats ----


@pytest.mark.asyncio
async def test_stats_track_in_out_of_scope() -> None:
    guard = TaskBoundaryGuard()
    scope = ScopeConfig(
        role_id="x",
        role_name="marketing",
        allowed_task_types=["marketing.copywriting"],
        forbidden_task_types=["coding.*"],
    )
    await guard.check({"task_type": "marketing.copywriting"}, scope=scope)  # in
    await guard.check({"task_type": "coding.python"}, scope=scope)  # out (blacklist)
    await guard.check({"task_type": "math.linear"}, scope=scope)  # heuristic 决定

    stats = guard.get_stats()
    assert stats["checks_total"] == 3
    assert stats["in_scope_count"] >= 1
    assert stats["out_of_scope_count"] >= 1


# ---- 校验 ----


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError):
        TaskBoundaryGuard(threshold=1.5)


def test_boundary_decision_score_clamp() -> None:
    """boundary_score 必须 0..1, BaseModel 校验."""
    with pytest.raises(Exception):
        BoundaryDecision(in_scope=True, boundary_score=1.5, reason="x")


# ---- 真实场景 OffTopicEval ----


@pytest.mark.asyncio
async def test_realistic_offtopic_eval_marketing_agent_rejects_coding() -> None:
    """模拟 OffTopicEval 主场景: 营销 agent 应该 reject 修 bug 任务."""
    guard = TaskBoundaryGuard(threshold=0.4)
    marketing_scope = ScopeConfig(
        role_id="marketing-agent",
        role_name="营销文案专员",
        allowed_task_types=[
            "marketing.copywriting",
            "marketing.ad",
            "marketing.social_media",
        ],
        forbidden_task_types=["coding.*", "math.*", "data.analysis"],
        boundary_strict_mode=True,
        out_of_scope_redirect="general-purpose-agent",
    )

    # 用户问 1: "帮我写广告" → in_scope
    d1 = await guard.check(
        {"task_type": "marketing.ad", "success_criteria_short": "帮我写一个广告"},
        scope=marketing_scope,
    )
    assert d1.in_scope is True

    # 用户问 2: "帮我修 bug" → reject (blacklist)
    d2 = await guard.check(
        {"task_type": "coding.python.fastapi", "success_criteria_short": "修 bug"},
        scope=marketing_scope,
    )
    assert d2.in_scope is False
    assert d2.suggested_redirect == "general-purpose-agent"

    # 用户问 3: "算 5+3" → reject (math.* blacklist)
    d3 = await guard.check(
        {"task_type": "math.arithmetic", "success_criteria_short": "算 5+3"},
        scope=marketing_scope,
    )
    assert d3.in_scope is False
