"""Tests for PanoramaBuilder (V2.1.2 §5.8.1)."""

from __future__ import annotations

import pytest
from kun.engineering.panorama_builder import (
    PREFLIGHT_MODULES,
    PanoramaBuilder,
    detect_tier,
)


def test_detect_tier_cache_hit_minimal() -> None:
    assert detect_tier({"cache_hit": True}) == "minimal"


def test_detect_tier_template_minimal() -> None:
    assert detect_tier({"chosen_template_ref": "tmpl-1"}) == "minimal"


def test_detect_tier_low_risk_simple_light() -> None:
    assert detect_tier({"risk_level": "low", "complexity_score": 0.1}) == "light"


def test_detect_tier_medium_complexity() -> None:
    assert detect_tier({"risk_level": "medium", "complexity_score": 0.5}) == "medium"


def test_detect_tier_critical_full() -> None:
    assert detect_tier({"risk_level": "critical", "complexity_score": 0.5}) == "full"


def test_detect_tier_high_complexity_full() -> None:
    assert detect_tier({"risk_level": "low", "complexity_score": 0.95}) == "full"


def test_preflight_modules_complete() -> None:
    """V2.1.2 §5.8.1 矩阵 10 个模块都注册."""
    expected = {
        "risk_assessment",
        "pre_conflict_scan",
        "task_split",
        "context_preheat",
        "resource_estimate",
        "attention_allocation",
        "role_instantiation",
        "multi_judge_review",
        "alternative_paths",
        "risk_graph",
    }
    assert set(PREFLIGHT_MODULES) == expected


@pytest.mark.asyncio
async def test_expand_minimal_skips_all_modules() -> None:
    """极简任务 (低 risk + 低 complexity) → 跳过所有事前模块."""
    builder = PanoramaBuilder()
    panorama = await builder.expand(
        {
            "task_id": "tk-min",
            "user_message": "你好",
            "intent_one_sentence": "打招呼",
            "risk_level": "low",
            "complexity_score": 0.1,
        }
    )
    assert panorama.intent_one_sentence == "打招呼"
    assert panorama.modules_run == []  # 没 runner 注册, 全跳
    assert len(panorama.modules_skipped) == len(PREFLIGHT_MODULES)


@pytest.mark.asyncio
async def test_expand_high_risk_runs_risk_module() -> None:
    """high risk → risk_assessment 该跑."""
    risk_call_count = 0

    async def risk_runner(meta):
        nonlocal risk_call_count
        risk_call_count += 1
        return {
            "financial_risk": 0.5,
            "irreversibility_risk": 0.7,
            "complexity_risk": 0.6,
            "overall_risk_level": "high",
        }

    builder = PanoramaBuilder(risk_runner=risk_runner)
    panorama = await builder.expand(
        {
            "task_id": "tk-h",
            "user_message": "重要任务",
            "intent_one_sentence": "重要操作",
            "risk_level": "high",
            "complexity_score": 0.4,
        }
    )
    assert risk_call_count == 1
    assert "risk_assessment" in panorama.modules_run
    assert panorama.risk_assessment is not None
    assert panorama.risk_assessment.overall_risk_level == "high"


@pytest.mark.asyncio
async def test_expand_complexity_triggers_split_and_preheat() -> None:
    """complexity 0.6 → split + preheat 都该跑."""

    async def split_runner(meta):
        return {
            "steps": [
                {"intent": "step1"},
                {"intent": "step2", "depends_on": [0]},
            ]
        }

    async def preheat_runner(meta):
        return {"pinned_assets": ["a", "b"], "depth": "shallow"}

    builder = PanoramaBuilder(
        split_runner=split_runner,
        preheat_runner=preheat_runner,
    )
    panorama = await builder.expand(
        {
            "task_id": "tk-c",
            "intent_one_sentence": "复杂任务",
            "risk_level": "low",
            "complexity_score": 0.6,
        }
    )
    assert "task_split" in panorama.modules_run
    assert "context_preheat" in panorama.modules_run
    assert len(panorama.execution_plan) == 2
    assert panorama.context_preheat is not None
    assert panorama.context_preheat.pinned_assets == ["a", "b"]


@pytest.mark.asyncio
async def test_expand_critical_runs_multi_judge() -> None:
    """critical + complexity 0.8 → multi_judge_review 跑."""

    async def mj_runner(meta):
        return {"judges": 3, "consensus": 0.92}

    async def alt_runner(meta):
        return {
            "alternatives": [
                {
                    "path_id": "a1",
                    "description": "alt 1",
                    "estimated_cost_usd": 0.05,
                    "estimated_duration_sec": 30.0,
                },
            ]
        }

    builder = PanoramaBuilder(
        multi_judge_runner=mj_runner,
        alternative_runner=alt_runner,
    )
    panorama = await builder.expand(
        {
            "task_id": "tk-crit",
            "intent_one_sentence": "critical task",
            "risk_level": "critical",
            "complexity_score": 0.8,
        }
    )
    assert "multi_judge_review" in panorama.modules_run
    assert panorama.multi_judge_review == {"judges": 3, "consensus": 0.92}
    assert "alternative_paths" in panorama.modules_run
    assert len(panorama.alternative_paths) == 1


@pytest.mark.asyncio
async def test_expand_runs_modules_in_parallel() -> None:
    """模块并行跑, 总耗时 ~ max(单模块) 不是 sum."""
    import asyncio
    import time

    async def slow_module(meta):
        await asyncio.sleep(0.05)
        return {}

    builder = PanoramaBuilder(
        risk_runner=slow_module,
        split_runner=slow_module,
        preheat_runner=slow_module,
        attention_runner=slow_module,
    )
    start = time.perf_counter()
    panorama = await builder.expand(
        {
            "task_id": "tk-par",
            "intent_one_sentence": "x",
            "risk_level": "high",
            "complexity_score": 0.8,  # 触发多模块
        }
    )
    elapsed = time.perf_counter() - start
    # 4 个模块各 50ms, 串行 200ms, 并行应 ~50ms (留 100ms 容差)
    assert elapsed < 0.15, f"並行失败, 耗时 {elapsed * 1000:.1f}ms"
    assert len(panorama.modules_run) >= 3


@pytest.mark.asyncio
async def test_expand_module_failure_non_fatal() -> None:
    """单模块失败不阻塞其他 (logger.exception, panorama 仍出)."""

    async def fail_runner(meta):
        raise RuntimeError("模拟失败")

    async def ok_runner(meta):
        return {"steps": [{"intent": "x"}]}

    builder = PanoramaBuilder(
        risk_runner=fail_runner,
        split_runner=ok_runner,
    )
    panorama = await builder.expand(
        {
            "task_id": "tk-fail",
            "intent_one_sentence": "test",
            "risk_level": "high",
            "complexity_score": 0.5,
        }
    )
    # 失败模块仍在 modules_run (尝试跑过) 但产出空, 不影响其他
    assert panorama.risk_assessment is None  # 失败 → 没填
    assert len(panorama.execution_plan) == 1  # ok_runner 成功


@pytest.mark.asyncio
async def test_expand_records_generated_in_ms() -> None:
    builder = PanoramaBuilder()
    panorama = await builder.expand(
        {
            "task_id": "tk-time",
            "intent_one_sentence": "x",
            "risk_level": "low",
        }
    )
    assert panorama.generated_in_ms >= 0


@pytest.mark.asyncio
async def test_expand_intent_runner_called_when_no_intent() -> None:
    """没 intent_one_sentence → 调 intent_runner."""
    called = []

    async def intent_runner(meta):
        called.append(meta)
        return {"intent_one_sentence": "推断的意图"}

    builder = PanoramaBuilder(intent_runner=intent_runner)
    panorama = await builder.expand(
        {
            "task_id": "tk-i",
            "user_message": "做点事",
            "risk_level": "low",
        }
    )
    assert len(called) == 1
    assert panorama.intent_one_sentence == "推断的意图"


@pytest.mark.asyncio
async def test_expand_attention_module_high_complexity() -> None:
    async def attention_runner(meta):
        return {
            "importance": 0.8,
            "complexity": 0.9,
            "urgency": 0.5,
            "risk": 0.4,
            "overall_score": 0.65,
            "chosen_model_tier": "top",
            "chosen_evaluation_tier": 2,
        }

    builder = PanoramaBuilder(attention_runner=attention_runner)
    panorama = await builder.expand(
        {
            "task_id": "tk-att",
            "intent_one_sentence": "x",
            "complexity_score": 0.8,
            "risk_level": "low",
        }
    )
    assert "attention_allocation" in panorama.modules_run
    assert panorama.attention_allocation is not None
    assert panorama.attention_allocation.chosen_model_tier == "top"
    assert panorama.attention_allocation.chosen_evaluation_tier == 2
