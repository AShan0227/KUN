"""V3-3 Hermes full-chain adapter tests.

These tests intentionally prove consumption, not just existence:
- orchestrator sends LLM execution prompts through Hermes;
- agent_loop sends skill inputs/results through Hermes;
- external API / agent formatting delegates to the adapter registry.
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.agent_loop import run_agent_loop
from kun.engineering.orchestrator import Orchestrator
from kun.interface.hermes import DefaultHermesAdapter, HermesEnvelope
from kun.interface.llm.base import LLMMessage, LLMRequest, LLMResponse, TaskProfile, UsageInfo
from kun.interface.llm.router import LLMRouter
from kun.interface.llm.stub_provider import StubProvider
from kun.skills.dispatcher import SkillResult, register


def _task_ref() -> TaskRef:
    owner = Owner(tenant_id="u-test", user_id="tester")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("写测试", owner),
            task_type="coding.test",
            risk_level="medium",
            complexity_score=0.5,
            owner=owner,
            success_criteria_short="确认 Hermes 真的参与执行链路",
        ),
        spec=TaskSpec(
            goal_detail="把 LLM prompt 和 skill I/O 都接入 Hermes",
            success_metrics=["prompt 包含 Hermes 契约", "skill 参数被 Hermes 适配后再执行"],
        ),
    )


@pytest.mark.unit
def test_hermes_llm_prompt_is_structured_packet() -> None:
    adapter = DefaultHermesAdapter()

    prompt = adapter.render_llm_step_prompt(
        base_prompt="请执行当前任务步骤。",
        task_id="task-1",
        task_type="education.curriculum",
        risk_level="low",
        step_description="设计课程大纲",
    )

    assert "[Hermes v3.3]" in prompt
    assert "target: llm" in prompt
    assert "task_type: education.curriculum" in prompt
    assert "hermes_contract" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hermes_external_translation_uses_adapter_registry() -> None:
    adapter = DefaultHermesAdapter()

    api_packet = await adapter.translate_external(
        target="api",
        payload={"task_id": "task-1", "value": 7},
        context={"path": "/v1/tasks", "method": "POST"},
    )
    agent_packet = await adapter.translate_external(
        target="external_agent",
        payload={"task_id": "task-1"},
        context={"method": "task.submit"},
    )

    assert isinstance(api_packet, HermesEnvelope)
    assert api_packet.format == "rest"
    assert '"path": "/v1/tasks"' in api_packet.rendered
    assert agent_packet.format == "agent"
    assert '"jsonrpc": "2.0"' in agent_packet.rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_loop_consumes_hermes_skill_input_and_result() -> None:
    """Hermes must change what the dispatcher sees, not merely log around it."""

    class _FakeHermes(DefaultHermesAdapter):
        async def adapt_skill_input(
            self,
            *,
            skill_id: str,
            params: dict[str, Any],
            context: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            translated = dict(params)
            translated["hermes_injected"] = context["task_id"] if context else "missing"
            return translated

    async def _fake_skill(params: dict[str, Any]) -> SkillResult:
        return SkillResult(skill_id="hermes-test-skill", ok=True, output=params)

    register("hermes-test-skill", _fake_skill)
    try:
        calls = {"n": 0}

        def _builder(req: LLMRequest) -> LLMResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                return LLMResponse(
                    content='<skill name="hermes-test-skill">{"x": 1}</skill>',
                    usage=UsageInfo(input_tokens=5, output_tokens=5),
                )
            user_text = "\n".join(m.content for m in req.messages if m.role == "user")
            assert "hermes_injected" in user_text
            assert "task-abc" in user_text
            assert '"hermes"' in user_text
            return LLMResponse(content="完成", usage=UsageInfo(input_tokens=5, output_tokens=5))

        stub = StubProvider(builder=_builder, latency_ms=0)
        router = LLMRouter({"top": stub, "cheap": stub, "fallback": stub})
        result = await run_agent_loop(
            router=router,
            purpose="execution",
            initial_request=LLMRequest(messages=[LLMMessage(role="user", content="run skill")]),
            max_iterations=3,
            hermes_adapter=_FakeHermes(),
            hermes_context={"task_id": "task-abc"},
        )

        assert result.final_answer == "完成"
        skill_result = result.iterations[0].skill_results[0]
        assert skill_result["output"]["hermes_injected"] == "task-abc"
        assert skill_result["metadata"]["hermes"]["kind"] == "skill_result"
    finally:
        from kun.skills import dispatcher as d

        d._REGISTRY.pop("hermes-test-skill", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_execute_step_consumes_hermes_prompt() -> None:
    """The real step executor must send Hermes-shaped prompt to the LLM."""

    seen_user_prompts: list[str] = []

    def _builder(req: LLMRequest) -> LLMResponse:
        user_prompt = "\n".join(m.content for m in req.messages if m.role == "user")
        seen_user_prompts.append(user_prompt)
        return LLMResponse(
            content="执行完成",
            usage=UsageInfo(input_tokens=10, output_tokens=4),
            model="stub-hermes",
            provider="stub",
            tier="cheap",
        )

    stub = StubProvider(builder=_builder, latency_ms=0)
    router = LLMRouter({"top": stub, "cheap": stub, "fallback": stub})
    orch = Orchestrator(llm_router=router)

    answer, response = await orch._execute_step(
        task_ref=_task_ref(),
        step_description="接入 Hermes",
        purpose="execution",
        profile=TaskProfile(task_type="coding.test", risk_level="medium"),
        pre_dispatched_block="\n预取结果: ok",
    )

    assert answer == "执行完成"
    assert response.content == "执行完成"
    assert seen_user_prompts
    prompt = seen_user_prompts[0]
    assert "[Hermes v3.3]" in prompt
    assert "task_packet:" in prompt
    assert "prefetched_tool_results:" in prompt
