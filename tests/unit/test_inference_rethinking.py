"""Wire 35: V2.2 §27 Inference-Time Rethinking 真触发."""

from __future__ import annotations

import json
from typing import Any

import pytest
from kun.engineering.execution_protocol import (
    ExecutionStep,
    StructuredStepGenerator,
    ThoughtActionConsistency,
)
from kun.interface.llm.base import LLMResponse, UsageInfo


class _ScriptedRouter:
    """Stub router — invoke() 按 invocations list 顺序返预设 ExecutionStep."""

    def __init__(self, scripted_steps: list[dict[str, Any]]) -> None:
        self._scripted = scripted_steps
        self.invocations: list[Any] = []

    async def invoke(self, request, *, purpose: str = "execution"):
        self.invocations.append(request)
        idx = min(len(self.invocations) - 1, len(self._scripted) - 1)
        step_dict = self._scripted[idx]
        return LLMResponse(
            content=json.dumps(step_dict),
            usage=UsageInfo(input_tokens=10, output_tokens=20),
        )


def _step_dict(action_type: str, thought: str, **kwargs) -> dict[str, Any]:
    return {
        "step_id": 1,
        "thought": thought,
        "action_type": action_type,
        "action_payload": kwargs.get("payload", {}),
        "expected_outcome": "ok",
        "confidence": kwargs.get("confidence", 0.8),
        "cost_estimate_usd": 0.01,
    }


# ---- 没装 consistency_checker → Wire 11 行为 ----


@pytest.mark.asyncio
async def test_no_consistency_checker_returns_first_step() -> None:
    """没装 ThoughtActionConsistency → 不 rethink, 跑一次返."""
    router = _ScriptedRouter([_step_dict("use_skill", "调用 skill 处理")])
    gen = StructuredStepGenerator(router)
    step = await gen.generate("test", {}, mode="SMART")
    assert step.action_type == "use_skill"
    assert len(router.invocations) == 1


# ---- 装 consistency_checker, consistency 高 → 不 rethink ----


@pytest.mark.asyncio
async def test_high_consistency_no_rethink() -> None:
    """thought 含 'skill' 关键词 + action_type=use_skill → consistency 高."""
    router = _ScriptedRouter([_step_dict("use_skill", "调用 skill 工具完成")])
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker)
    step = await gen.generate("test", {}, mode="SMART")

    assert step.thought_action_consistency >= 0.5
    assert step.rethink_count == 0
    assert len(router.invocations) == 1


# ---- consistency 低 → 触发 rethink ----


@pytest.mark.asyncio
async def test_low_consistency_triggers_rethink() -> None:
    """thought 跟 action_type 完全不匹配 → 第一次 consistency 低 → rethink, 第二次返高."""
    router = _ScriptedRouter(
        [
            _step_dict("use_memory", "瞎说一通无关的"),  # 第 1 次: thought 没 memory keyword
            _step_dict("use_memory", "回顾历史记忆"),  # 第 2 次: thought 含 memory keyword
        ]
    )
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=2)
    step = await gen.generate("test", {}, mode="SMART")

    # 应该触发了至少 1 次 rethink
    assert len(router.invocations) >= 2
    assert step.rethink_count >= 1
    # 第二次 thought 含关键词 → consistency 应该高
    assert step.thought_action_consistency >= 0.5


@pytest.mark.asyncio
async def test_max_rethinks_exhausted_returns_final_step() -> None:
    """连续 N 次 consistency 都低 → max_rethinks 用完后返最后一次 (不抛)."""
    router = _ScriptedRouter(
        [
            _step_dict("use_memory", "瞎说 1"),
            _step_dict("use_memory", "瞎说 2"),
            _step_dict("use_memory", "瞎说 3"),
        ]
    )
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=2)
    step = await gen.generate("test", {}, mode="SMART")

    # max_rethinks=2 → 最多调 3 次 (第 1 次 + 2 次 rethink)
    assert len(router.invocations) == 3
    assert step.rethink_count == 2
    assert step.thought_action_consistency < 0.5  # 仍然低 (但不抛)


@pytest.mark.asyncio
async def test_rethink_hint_contains_prior_thought() -> None:
    """第 2 次 generate 的 request messages 应该含 'RETHINK' 提示 + 上轮 thought."""
    router = _ScriptedRouter(
        [
            _step_dict("use_memory", "瞎说一通"),
            _step_dict("use_memory", "回顾记忆"),
        ]
    )
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=1)
    await gen.generate("test", {}, mode="SMART")

    assert len(router.invocations) == 2
    # 第 2 次 request 应该有 RETHINK hint system message
    second_request = router.invocations[1]
    rethink_msgs = [
        m for m in second_request.messages if m.role == "system" and "RETHINK" in m.content
    ]
    assert len(rethink_msgs) == 1
    assert "瞎说一通" in rethink_msgs[0].content  # 含上轮 thought


@pytest.mark.asyncio
async def test_max_rethinks_zero_disables_rethinking() -> None:
    """max_rethinks=0 → 即使 consistency 低也不重试."""
    router = _ScriptedRouter([_step_dict("use_memory", "瞎说")])
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=0)
    step = await gen.generate("test", {}, mode="SMART")

    assert len(router.invocations) == 1
    assert step.rethink_count == 0
    assert step.thought_action_consistency < 0.5  # 低但不重试


@pytest.mark.asyncio
async def test_fast_mode_skips_rethink() -> None:
    """FAST 模式直接走 fallback, 不调 LLM, 不 rethink."""
    router = _ScriptedRouter([_step_dict("use_memory", "x")])
    checker = ThoughtActionConsistency()
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=2)
    step = await gen.generate("test", {}, mode="FAST")

    assert len(router.invocations) == 0  # 没调 LLM
    assert step.rethink_count == 0


@pytest.mark.asyncio
async def test_rethink_count_passed_into_step() -> None:
    """成功 step 的 rethink_count 字段反映实际重试次数."""
    router = _ScriptedRouter(
        [
            _step_dict("use_memory", "瞎说"),  # 第 1 次低
            _step_dict("use_memory", "回顾历史记忆"),  # 第 2 次高
        ]
    )
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=2)
    step = await gen.generate("test", {}, mode="SMART")

    assert step.rethink_count == 1


@pytest.mark.asyncio
async def test_unparseable_response_no_rethink_fallback() -> None:
    """LLM 返不可解析 → fallback step 直接返, 不 rethink (避免无限循环)."""

    class _BadRouter:
        def __init__(self) -> None:
            self.invocations: list[Any] = []

        async def invoke(self, request, *, purpose: str = "execution"):
            self.invocations.append(request)
            return LLMResponse(content="this is not json", usage=UsageInfo())

    router = _BadRouter()
    checker = ThoughtActionConsistency()
    gen = StructuredStepGenerator(router, consistency_checker=checker, max_rethinks=2)
    step = await gen.generate("test", {}, mode="SMART")

    assert isinstance(step, ExecutionStep)
    assert (
        "fallback" in step.expected_outcome.lower()
        or "unparseable" in step.expected_outcome.lower()
        or step.action_type == "direct_llm"
    )
    # 1 次 LLM call (没 rethink, fallback 直接返)
    assert len(router.invocations) == 1
