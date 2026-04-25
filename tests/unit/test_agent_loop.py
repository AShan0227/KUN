"""Agent loop tests — parse / dispatch / formatting."""

from __future__ import annotations

import pytest
from kun.engineering.agent_loop import (
    build_skill_directive,
    format_tool_results,
    parse_skill_calls,
)
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.skills.dispatcher import autoload_builtins, register
from kun.skills.dispatcher import dispatch as _dispatch  # noqa: F401 — registers builtins


@pytest.fixture(autouse=True)
def _ensure_builtins() -> None:
    autoload_builtins()


@pytest.mark.unit
def test_parse_skill_calls_finds_one_block() -> None:
    text = """
    需要查一下最新的进度。
    <skill name="web-search">{"query": "kun project"}</skill>
    然后我会基于结果继续。
    """
    calls = parse_skill_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "web-search"
    assert calls[0].params == {"query": "kun project"}


@pytest.mark.unit
def test_parse_skill_calls_skips_unknown_skill() -> None:
    text = '<skill name="this-doesnt-exist">{"x": 1}</skill>'
    assert parse_skill_calls(text) == []


@pytest.mark.unit
def test_parse_skill_calls_handles_multiple_blocks() -> None:
    text = """
    <skill name="python-exec">{"code": "print(1)"}</skill>
    <skill name="shell-exec">{"command": "echo hi"}</skill>
    """
    calls = parse_skill_calls(text)
    assert [c.name for c in calls] == ["python-exec", "shell-exec"]


@pytest.mark.unit
def test_parse_skill_calls_skips_invalid_json() -> None:
    text = '<skill name="python-exec">{ this is not json }</skill>'
    assert parse_skill_calls(text) == []


@pytest.mark.unit
def test_build_skill_directive_lists_skills_with_schema() -> None:
    out = build_skill_directive(
        [
            ("python-exec", "Run Python code", {"code": "string"}),
            ("web-search", "Search the web", {}),
        ]
    )
    assert "python-exec" in out
    assert "web-search" in out
    assert '<skill name="工具名">' in out
    assert '"code": "string"' in out


@pytest.mark.unit
def test_build_skill_directive_empty_when_no_skills() -> None:
    assert build_skill_directive([]) == ""


@pytest.mark.unit
def test_format_tool_results_includes_skill_id_and_status() -> None:
    text = format_tool_results(
        [
            {"skill_id": "python-exec", "ok": True, "output": {"stdout": "42"}},
            {"skill_id": "web-search", "ok": False, "error": "rate limited"},
        ]
    )
    assert "python-exec" in text
    assert "web-search" in text
    assert "rate limited" in text
    assert "42" in text


@pytest.mark.unit
def test_format_tool_results_truncates_long_output() -> None:
    big = {"data": "x" * 5000}
    text = format_tool_results([{"skill_id": "csv-query", "ok": True, "output": big}])
    assert "(truncated)" in text


@pytest.mark.unit
def test_register_dispatch_round_trip() -> None:
    """Manual register works for an externally-defined skill."""
    from kun.skills.dispatcher import SkillResult, is_registered

    async def _fake_skill(params: dict) -> SkillResult:
        return SkillResult(skill_id="fake", ok=True, output=params.get("v"))

    register("test-fake-skill", _fake_skill)
    try:
        assert is_registered("test-fake-skill")
        text = '<skill name="test-fake-skill">{"v": 7}</skill>'
        calls = parse_skill_calls(text)
        assert len(calls) == 1
        assert calls[0].params == {"v": 7}
    finally:
        # Restore registry — direct mutation acceptable for tests
        from kun.skills import dispatcher as d

        d._REGISTRY.pop("test-fake-skill", None)


# ============== 端到端: LLM 出 <skill> → loop → dispatch → 回喂 ==============


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_loop_e2e_skill_dispatch_then_final_answer() -> None:
    """模拟真实 ReAct: 第一轮 LLM 出 <skill> 块, 第二轮看到结果给最终答案.

    这是小尾巴 A 的验证 — 不靠真 API key, 用 stub 的 builder 注入 LLM
    的多轮回复, 验证 agent_loop 的解析 / dispatch / 回喂链路全部对.
    """
    from kun.engineering.agent_loop import run_agent_loop
    from kun.interface.llm.base import LLMResponse, UsageInfo
    from kun.interface.llm.router import LLMRouter
    from kun.interface.llm.stub_provider import StubProvider

    call_counter = {"n": 0}

    def react_builder(req):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            # 第一轮: LLM 决定调 python-exec
            content = (
                "我先跑一段代码确认.\n"
                '<skill name="python-exec">{"code": "print(2+2)", "timeout_sec": 5}</skill>'
            )
        else:
            # 第二轮: 看到 stdout=4, 给最终答案 (不再带 <skill>)
            user_text = " ".join(m.content for m in req.messages if m.role == "user")
            assert "stdout" in user_text and "4" in user_text, (
                "第二轮 prompt 里必须能看到上一轮 dispatch 拿到的结果"
            )
            content = "答案: 2+2 = 4."
        return LLMResponse(
            content=content,
            usage=UsageInfo(input_tokens=20, output_tokens=10),
            model="stub-react",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    stub = StubProvider(builder=react_builder, latency_ms=0.0)
    fb = StubProvider(model_id="fb", tier="fallback", latency_ms=0.0)
    # 把 cheap 也注册成 stub, 避开 complexity 降级走到 fallback 的坑.
    router = LLMRouter({"top": stub, "cheap": stub, "fallback": fb})

    initial = LLMRequest(
        messages=[
            LLMMessage(
                role="system",
                content=build_skill_directive(
                    [("python-exec", "Run Python code", {"code": "string"})]
                ),
            ),
            LLMMessage(role="user", content="算 2+2 是多少?"),
        ],
    )
    result = await run_agent_loop(
        router=router,
        purpose="execution",
        initial_request=initial,
        max_iterations=3,
    )

    # LLM 真的被调了两轮
    assert call_counter["n"] == 2
    # 第一轮有 1 个 skill call
    assert len(result.iterations) == 2
    assert len(result.iterations[0].skill_calls) == 1
    assert result.iterations[0].skill_calls[0].name == "python-exec"
    # 第一轮 dispatch 真跑了 python-exec, 结果非空
    assert len(result.iterations[0].skill_results) == 1
    py_result = result.iterations[0].skill_results[0]
    assert py_result["ok"] is True
    assert "4" in py_result["output"]["stdout"]
    # 第二轮没有 <skill> 块 → loop 终止
    assert result.iterations[1].skill_calls == []
    # 最终答案不带 <skill> 标签
    assert "<skill" not in result.final_answer
    assert "4" in result.final_answer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_loop_no_skill_block_returns_immediately() -> None:
    """LLM 不出 <skill> 块 → loop 一次就返回, 不画蛇添足."""
    from kun.engineering.agent_loop import run_agent_loop
    from kun.interface.llm.base import LLMResponse, UsageInfo
    from kun.interface.llm.router import LLMRouter
    from kun.interface.llm.stub_provider import StubProvider

    call_counter = {"n": 0}

    def direct_builder(_req):
        call_counter["n"] += 1
        return LLMResponse(
            content="纯文字答案, 不需要工具.",
            usage=UsageInfo(input_tokens=10, output_tokens=5),
            model="stub-direct",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    stub = StubProvider(builder=direct_builder, latency_ms=0.0)
    router = LLMRouter(
        {
            "top": stub,
            "cheap": stub,
            "fallback": StubProvider(model_id="fb", tier="fallback", latency_ms=0.0),
        }
    )
    initial = LLMRequest(messages=[LLMMessage(role="user", content="打个招呼")])

    result = await run_agent_loop(
        router=router,
        purpose="execution",
        initial_request=initial,
        max_iterations=3,
    )
    assert call_counter["n"] == 1
    assert len(result.iterations) == 1
    assert result.final_answer == "纯文字答案, 不需要工具."
