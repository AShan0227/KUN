from __future__ import annotations

import pytest
from kun.engineering.execution_protocol import ExecutionStep, ThoughtActionConsistency


@pytest.mark.asyncio
async def test_smart_mode_uses_lite_judge() -> None:
    calls: list[str] = []

    async def full(_thought: str, _action_type: str) -> float:
        calls.append("full")
        return 0.9

    async def lite(_thought: str, _action_type: str) -> float:
        calls.append("lite")
        return 0.8

    checker = ThoughtActionConsistency(llm_judge=full, lite_llm_judge=lite)
    step = ExecutionStep(
        step_id=1,
        thought="需要处理一下",
        action_type="use_skill",
        action_payload={},
        expected_outcome="ok",
    )

    score, reason = await checker.check(step, mode="SMART")

    assert score == 0.8
    assert "lite_jury" in reason
    assert calls == ["lite"]


@pytest.mark.asyncio
async def test_max_mode_uses_full_judge() -> None:
    calls: list[str] = []

    async def full(_thought: str, _action_type: str) -> float:
        calls.append("full")
        return 0.9

    async def lite(_thought: str, _action_type: str) -> float:
        calls.append("lite")
        return 0.8

    checker = ThoughtActionConsistency(llm_judge=full, lite_llm_judge=lite)
    step = ExecutionStep(
        step_id=1,
        thought="需要处理一下",
        action_type="web_search",
        action_payload={},
        expected_outcome="ok",
    )

    score, reason = await checker.check(step, mode="MAX")

    assert score == 0.9
    assert "llm_judge" in reason
    assert calls == ["full"]
