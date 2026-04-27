from __future__ import annotations

import pytest
from kun.engineering.execution_protocol import (
    ExecutionStep,
    StructuredStepGenerator,
    ThoughtActionConsistency,
    make_jury_consistency_judge,
)
from kun.interface.llm import LLMResponse


class FakeRouter:
    def __init__(self, response, *, judge_response: LLMResponse | None = None):
        self.response = response
        self.judge_response = judge_response or LLMResponse(
            content='{"pass": true, "score": 0.9, "reason": "一致"}'
        )
        self.calls = []

    async def invoke(self, request, *, purpose: str):
        self.calls.append((request, purpose))
        if purpose == "judge":
            return self.judge_response
        return self.response


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_mode_skips_router_and_returns_direct_llm_step():
    router = FakeRouter({"action_type": "web_search"})
    step = await StructuredStepGenerator(router).generate("say hi", {"step_id": 7}, mode="FAST")

    assert router.calls == []
    assert step.step_id == 7
    assert step.action_type == "direct_llm"
    assert step.action_payload["prompt"] == "say hi"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_smart_mode_parses_json_content_and_sets_request_contract():
    response = LLMResponse(
        content=(
            '{"step_id": 2, "thought": "Need memory", "action_type": "use_memory", '
            '"action_payload": {"query": "profile"}, "expected_outcome": "Relevant memory", '
            '"confidence": 0.8, "cost_estimate_usd": 0.01}'
        )
    )
    router = FakeRouter(response)

    step = await StructuredStepGenerator(router).generate("remember me", {"risk_level": "low"})

    assert step.action_type == "use_memory"
    assert step.action_payload == {"query": "profile"}
    assert step.confidence == 0.8
    assert router.calls[0][1] == "execution"
    request = router.calls[0][0]
    assert "Return exactly one JSON object" in request.messages[0].content
    assert request.profile.prefer_speed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_mode_parses_dict_response_and_requests_reasoning_profile():
    router = FakeRouter(
        {
            "step_id": 3,
            "thought": "Need clarification",
            "action_type": "ask_user",
            "action_payload": {"question": "Which repo?"},
            "expected_outcome": "Disambiguated target",
        }
    )

    step = await StructuredStepGenerator(router).generate("fix it", {}, mode="MAX")

    assert step.step_id == 3
    assert step.action_type == "ask_user"
    assert router.calls[0][0].profile.needs_reasoning is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confidence_is_clamped_high():
    router = FakeRouter(
        {
            "step_id": 1,
            "thought": "Search",
            "action_type": "web_search",
            "action_payload": {},
            "expected_outcome": "Facts",
            "confidence": 9,
        }
    )

    step = await StructuredStepGenerator(router).generate("latest?", {}, mode="SMART")

    assert step.confidence == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confidence_is_clamped_low():
    router = FakeRouter(
        {
            "step_id": 1,
            "thought": "Search",
            "action_type": "web_search",
            "action_payload": {},
            "expected_outcome": "Facts",
            "confidence": -0.4,
        }
    )

    step = await StructuredStepGenerator(router).generate("latest?", {}, mode="SMART")

    assert step.confidence == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_confidence_uses_default():
    router = FakeRouter(
        {
            "step_id": 1,
            "thought": "Answer",
            "action_type": "direct_llm",
            "action_payload": {},
            "expected_outcome": "Answer",
            "confidence": "not-a-number",
        }
    )

    step = await StructuredStepGenerator(router).generate("x", {}, mode="SMART")

    assert step.confidence == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_negative_cost_estimate_is_clamped_to_zero():
    router = FakeRouter(
        {
            "step_id": 1,
            "thought": "Answer",
            "action_type": "direct_llm",
            "action_payload": {},
            "expected_outcome": "Answer",
            "cost_estimate_usd": -2,
        }
    )

    step = await StructuredStepGenerator(router).generate("x", {}, mode="SMART")

    assert step.cost_estimate_usd == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_fields_fall_back_to_direct_llm_defaults():
    router = FakeRouter({"thought": "Partial schema"})

    step = await StructuredStepGenerator(router).generate("do work", {}, mode="SMART")

    assert step.step_id == 1
    assert step.thought == "Partial schema"
    assert step.action_type == "direct_llm"
    assert step.action_payload == {}
    assert step.expected_outcome == "Answer the prompt directly."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unparseable_json_response_returns_reasonable_fallback():
    router = FakeRouter(LLMResponse(content="not json at all"))

    step = await StructuredStepGenerator(router).generate("do work", {}, mode="SMART")

    assert step.action_type == "direct_llm"
    assert step.thought == "Use direct LLM execution (unparseable_llm_response)."
    assert step.action_payload["prompt"] == "do work"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_embedded_in_prose_is_accepted():
    router = FakeRouter(
        LLMResponse(
            content=(
                'Here: {"step_id": 4, "thought": "Use skill", "action_type": "use_skill", '
                '"action_payload": {"skill": "docs"}, "expected_outcome": "Draft"}'
            )
        )
    )

    step = await StructuredStepGenerator(router).generate("draft docs", {}, mode="SMART")

    assert step.step_id == 4
    assert step.action_type == "use_skill"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_step_shape_is_accepted():
    router = FakeRouter(
        {
            "step": {
                "step_id": 5,
                "thought": "Search first",
                "action_type": "web_search",
                "action_payload": {"query": "KUN"},
                "expected_outcome": "External context",
            }
        }
    )

    step = await StructuredStepGenerator(router).generate("research", {}, mode="SMART")

    assert step.step_id == 5
    assert step.action_payload == {"query": "KUN"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_action_type_is_downgraded_to_direct_llm():
    router = FakeRouter(
        {
            "step_id": 1,
            "thought": "Try unsupported tool",
            "action_type": "shell",
            "action_payload": "ls",
            "expected_outcome": "Files",
        }
    )

    step = await StructuredStepGenerator(router).generate("list files", {}, mode="SMART")

    assert step.action_type == "direct_llm"
    assert step.action_payload == {"value": "ls"}


@pytest.mark.unit
def test_execution_step_model_clamps_boundaries_directly():
    step = ExecutionStep(
        step_id=1,
        thought="x",
        action_type="direct_llm",
        action_payload={},
        expected_outcome="y",
        confidence=2,
        cost_estimate_usd=-1,
    )

    assert step.confidence == 1.0
    assert step.cost_estimate_usd == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_consistency_judge_uses_configured_judges():
    router = FakeRouter({})
    judge = make_jury_consistency_judge(router, judge_count=3)

    score = await judge("Need memory", "use_memory")

    assert score == 0.9
    assert [purpose for _, purpose in router.calls] == ["judge", "judge", "judge"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_consistency_judge_clamps_failed_verdict_below_threshold():
    router = FakeRouter(
        {},
        judge_response=LLMResponse(content='{"pass": false, "score": 0.8, "reason": "不一致"}'),
    )
    judge = make_jury_consistency_judge(router, judge_count=5)

    score = await judge("Need memory", "web_search")

    assert score == 0.49
    assert [purpose for _, purpose in router.calls].count("judge") == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_mode_rethinks_with_injected_jury_judge():
    responses = [
        {
            "step_id": 1,
            "thought": "Need memory",
            "action_type": "web_search",
            "action_payload": {"query": "profile"},
            "expected_outcome": "External context",
        },
        {
            "step_id": 2,
            "thought": "Need web search",
            "action_type": "web_search",
            "action_payload": {"query": "profile"},
            "expected_outcome": "External context",
        },
    ]

    class SequenceRouter(FakeRouter):
        async def invoke(self, request, *, purpose: str):
            self.calls.append((request, purpose))
            if purpose == "judge":
                return LLMResponse(content='{"pass": false, "score": 0.2, "reason": "不一致"}')
            return responses.pop(0)

    router = SequenceRouter({})
    checker = ThoughtActionConsistency(
        consistency_threshold=0.5,
        llm_judge=make_jury_consistency_judge(router, judge_count=5),
    )

    step = await StructuredStepGenerator(
        router,
        consistency_checker=checker,
        max_rethinks=1,
    ).generate("fix it", {}, mode="MAX")

    assert step.rethink_count == 1
    assert step.thought_action_consistency == 0.9
    execution_calls = [purpose for _, purpose in router.calls if purpose == "execution"]
    assert len(execution_calls) == 2
    assert [purpose for _, purpose in router.calls].count("judge") == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_style_checker_uses_jury_without_generator_special_case():
    router = FakeRouter({})
    checker = ThoughtActionConsistency(
        consistency_threshold=0.5,
        llm_judge=make_jury_consistency_judge(router, judge_count=5),
    )

    score, reason = await checker.check(
        ExecutionStep(
            step_id=1,
            thought="Need memory",
            action_type="web_search",
            action_payload={},
            expected_outcome="External context",
        )
    )

    assert score == 0.9
    assert reason == "llm_judge:0.90"
    assert [purpose for _, purpose in router.calls].count("judge") == 5
