"""LT.E — ExecutorLoop 单测 (multi-step agent loop, 整合 LT.B/C/D)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.executor.checkpoint import (
    TaskCheckpoint,
    TaskCheckpointService,
)
from kun.agents.executor.compaction import ConversationCompactor
from kun.agents.executor.exec_loop import (
    ExecutorLoop,
    LLMStepResponse,
    LoopResult,
    ToolCall,
    ToolResult,
)
from kun.agents.supervisor.plan_review_heartbeat import PlanReviewHeartbeat
from kun.agents.supervisor.plan_review_service import PlanReviewService

# ---- Stubs ----


def _final_response(text: str = "done", *, cost: float = 0.01, tokens: int = 100) -> LLMStepResponse:
    return LLMStepResponse(
        content=text, tool_calls=[], finish_reason="end_turn",
        cost_usd=cost, usage_tokens=tokens,
    )


def _tool_response(tool_name: str = "calculator", tool_id: str = "t-1",
                   args: dict | None = None, *, cost: float = 0.005, tokens: int = 50) -> LLMStepResponse:
    return LLMStepResponse(
        content="",
        tool_calls=[ToolCall(tool_id=tool_id, name=tool_name, arguments=args or {})],
        finish_reason="tool_use",
        cost_usd=cost, usage_tokens=tokens,
    )


class _FakeLLM:
    """Stateful fake LLM. Returns responses in order, then loops to last."""

    def __init__(self, responses: list[LLMStepResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self._idx = 0

    async def __call__(self, messages: list[dict[str, Any]]) -> LLMStepResponse:
        self.calls.append([dict(m) for m in messages])
        if self._idx < len(self.responses):
            r = self.responses[self._idx]
            self._idx += 1
            return r
        return self.responses[-1]


async def _passthrough_tool_executor(calls: list[ToolCall]) -> list[ToolResult]:
    """Each tool returns f'ok:{name}({args})' as content."""
    return [
        ToolResult(tool_id=c.tool_id, content=f"ok:{c.name}({c.arguments})")
        for c in calls
    ]


async def _all_error_tool_executor(calls: list[ToolCall]) -> list[ToolResult]:
    return [
        ToolResult(tool_id=c.tool_id, content="boom", is_error=True)
        for c in calls
    ]


class _FakeCheckpointStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.status_marks: list[tuple[str, str, str]] = []

    async def writer(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))

    async def reader(self, tenant_id: str, task_id: str) -> TaskCheckpoint | None:
        return None  # not used in these tests

    async def marker(self, tenant_id: str, cp_id: str, status: str) -> None:
        self.status_marks.append((tenant_id, cp_id, status))


def _make_loop(
    *,
    llm: _FakeLLM,
    tool_executor=_passthrough_tool_executor,
    max_steps: int = 30,
    max_budget_usd: float = 1.0,
    max_wall_seconds: float = 1800.0,
    max_consecutive_tool_failures: int = 3,
    checkpoint: TaskCheckpointService | None = None,
    plan_review: PlanReviewService | None = None,
    compactor: ConversationCompactor | None = None,
) -> ExecutorLoop:
    return ExecutorLoop(
        llm_invoker=llm,
        tool_executor=tool_executor,
        max_steps=max_steps,
        max_budget_usd=max_budget_usd,
        max_wall_seconds=max_wall_seconds,
        max_consecutive_tool_failures=max_consecutive_tool_failures,
        checkpoint_service=checkpoint,
        plan_review_service=plan_review,
        compactor=compactor,
    )


# ---- Construction ----


def test_constructor_validates_params() -> None:
    llm = _FakeLLM([_final_response()])
    with pytest.raises(ValueError, match="max_steps"):
        ExecutorLoop(
            llm_invoker=llm,
            tool_executor=_passthrough_tool_executor,
            max_steps=0,
        )
    with pytest.raises(ValueError, match="max_budget_usd"):
        ExecutorLoop(
            llm_invoker=llm,
            tool_executor=_passthrough_tool_executor,
            max_budget_usd=0,
        )
    with pytest.raises(ValueError, match="max_wall_seconds"):
        ExecutorLoop(
            llm_invoker=llm,
            tool_executor=_passthrough_tool_executor,
            max_wall_seconds=0,
        )
    with pytest.raises(ValueError, match="max_consecutive_tool"):
        ExecutorLoop(
            llm_invoker=llm,
            tool_executor=_passthrough_tool_executor,
            max_consecutive_tool_failures=0,
        )


# ---- Basic flow: 0 tool calls (final immediately) ----


@pytest.mark.asyncio
async def test_run_final_on_first_response() -> None:
    llm = _FakeLLM([_final_response("hello")])
    loop = _make_loop(llm=llm)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(result, LoopResult)
    assert result.status == "final"
    assert result.final_text == "hello"
    assert result.steps_taken == 1
    assert result.total_cost_usd == pytest.approx(0.01)
    assert result.total_tokens == 100


# ---- Tool call flow ----


@pytest.mark.asyncio
async def test_tool_call_then_final() -> None:
    llm = _FakeLLM([
        _tool_response("calculator", "t-1", {"x": 1}),
        _final_response("42"),
    ])
    loop = _make_loop(llm=llm)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "compute"}],
    )
    assert result.status == "final"
    assert result.final_text == "42"
    assert result.steps_taken == 2  # 1 tool step + 1 final step
    # Tool result should be in messages
    tool_msgs = [m for m in result.final_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "ok:calculator" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_response() -> None:
    multi_tool = LLMStepResponse(
        content="",
        tool_calls=[
            ToolCall(tool_id="t-1", name="search", arguments={"q": "a"}),
            ToolCall(tool_id="t-2", name="search", arguments={"q": "b"}),
        ],
        finish_reason="tool_use",
        cost_usd=0.01,
    )
    llm = _FakeLLM([multi_tool, _final_response("done")])
    loop = _make_loop(llm=llm)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "search both"}],
    )
    assert result.status == "final"
    tool_msgs = [m for m in result.final_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2


# ---- Termination by max_steps ----


@pytest.mark.asyncio
async def test_max_steps_termination() -> None:
    """LLM 永远要工具 — 撞 max_steps."""
    llm = _FakeLLM([_tool_response("x", "t-1") for _ in range(20)])
    loop = _make_loop(llm=llm, max_steps=3)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "go"}],
    )
    assert result.status == "max_steps"
    assert result.steps_taken == 3


# ---- Termination by budget ----


@pytest.mark.asyncio
async def test_budget_exceeded_termination() -> None:
    """每步 cost 0.4 → 第 3 步检查时 1.2 > budget 1.0 → 退出."""
    expensive = _tool_response("x", "t-1", cost=0.4)
    llm = _FakeLLM([expensive] * 10)
    loop = _make_loop(llm=llm, max_budget_usd=1.0)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "go"}],
    )
    assert result.status == "budget_exceeded"


# ---- Stuck detection ----


@pytest.mark.asyncio
async def test_stuck_after_consecutive_tool_failures() -> None:
    """连续 3 次 tool 失败 → stuck."""
    llm = _FakeLLM([_tool_response("x", f"t-{i}") for i in range(10)])
    loop = _make_loop(
        llm=llm, tool_executor=_all_error_tool_executor,
        max_consecutive_tool_failures=3,
    )
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "go"}],
    )
    assert result.status == "stuck"
    assert result.steps_taken >= 3


@pytest.mark.asyncio
async def test_consecutive_fails_resets_on_success() -> None:
    """2 fail + 1 success + ... 不应该 stuck (重置 counter)."""
    call_count = {"n": 0}

    async def alternating_executor(calls):
        call_count["n"] += 1
        if call_count["n"] in (1, 2):
            return [ToolResult(tool_id=c.tool_id, content="boom", is_error=True) for c in calls]
        return [ToolResult(tool_id=c.tool_id, content="ok") for c in calls]

    llm = _FakeLLM([
        _tool_response("x", "t-1"),
        _tool_response("x", "t-2"),
        _tool_response("x", "t-3"),  # success resets counter
        _final_response("done"),
    ])
    loop = _make_loop(
        llm=llm, tool_executor=alternating_executor,
        max_consecutive_tool_failures=3,
    )
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
    )
    assert result.status == "final"


# ---- LLM invocation failure ----


@pytest.mark.asyncio
async def test_llm_exception_returns_failed() -> None:
    """llm_invoker 抛 → status='failed', 不上抛."""

    async def bad_llm(messages):
        raise RuntimeError("anthropic 500")

    loop = _make_loop(llm=bad_llm)  # type: ignore[arg-type]
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "x"}],
    )
    assert result.status == "failed"
    assert result.error == "anthropic 500"
    assert "anthropic 500" in result.rationale


# ---- Tool executor exception ----


@pytest.mark.asyncio
async def test_tool_executor_exception_does_not_kill_loop() -> None:
    """tool_executor 抛异常 → 视为全 error, loop 继续."""
    call_count = {"n": 0}

    async def crash_then_ok(calls):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("tool crashed")
        return [ToolResult(tool_id=c.tool_id, content="ok") for c in calls]

    llm = _FakeLLM([
        _tool_response("x", "t-1"),
        _tool_response("x", "t-2"),
        _final_response("done"),
    ])
    loop = _make_loop(llm=llm, tool_executor=crash_then_ok)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
    )
    assert result.status == "final"
    # Error message should appear in tool result content
    tool_msgs = [m for m in result.final_messages if m.get("role") == "tool"]
    assert any("tool_executor error" in m.get("content", "") for m in tool_msgs)


# ---- Checkpoint integration ----


@pytest.mark.asyncio
async def test_checkpoint_saves_per_step() -> None:
    store = _FakeCheckpointStore()
    checkpoint = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )
    llm = _FakeLLM([
        _tool_response("x", "t-1"),
        _final_response("done"),
    ])
    loop = _make_loop(llm=llm, checkpoint=checkpoint)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "go"}],
        goal_anchor_id="ga-x",
    )
    assert result.status == "final"
    # 至少 2 checkpoint: 1 after tool step + 1 final
    assert len(store.rows) >= 2
    final_rows = [r for r in store.rows if r["status"] == "final"]
    assert len(final_rows) == 1
    assert final_rows[0]["goal_anchor_id"] == "ga-x"
    assert result.last_checkpoint_id is not None


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_break_loop() -> None:
    """checkpoint.save 抛 → loop 继续, last_checkpoint_id 可能 None."""

    async def bad_writer(_row):
        raise RuntimeError("db down")

    async def good_reader(_a, _b):
        return None

    checkpoint = TaskCheckpointService(writer=bad_writer, reader=good_reader)
    llm = _FakeLLM([_final_response("done")])
    loop = _make_loop(llm=llm, checkpoint=checkpoint)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
    )
    assert result.status == "final"
    assert result.last_checkpoint_id is None  # save failed, no id returned


# ---- Compactor integration ----


@pytest.mark.asyncio
async def test_compactor_runs_before_llm() -> None:
    """Compactor 在 LLM call 前跑."""
    compactor = ConversationCompactor(token_threshold=10, keep_last_k=1, protect_first_n=1)
    llm = _FakeLLM([_final_response("done")])
    loop = _make_loop(llm=llm, compactor=compactor)
    big_messages = [{"role": "user", "content": "x" * 200}] * 10
    await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=big_messages,
    )
    # LLM 第一个 call 的 messages 应该是 compacted 后的 (含 summary)
    first_call_msgs = llm.calls[0]
    assert any(m.get("_kun_compacted") for m in first_call_msgs)


@pytest.mark.asyncio
async def test_compactor_failure_does_not_break_loop() -> None:
    class _BadCompactor:
        async def maybe_compact(self, messages, anchor=None):
            raise RuntimeError("summarizer down")

    llm = _FakeLLM([_final_response("done")])
    loop = _make_loop(llm=llm, compactor=_BadCompactor())  # type: ignore[arg-type]
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "x"}],
    )
    assert result.status == "final"


# ---- PlanReviewService integration ----


@pytest.mark.asyncio
async def test_plan_review_prompt_injected_when_due() -> None:
    heartbeat = PlanReviewHeartbeat(step_interval=1, time_interval_sec=3600)
    plan_review = PlanReviewService(heartbeat=heartbeat)
    # 第一 step 是 tool_call, 之后 heartbeat 触发 → 下一次 LLM call 前注入 prompt
    llm = _FakeLLM([
        _tool_response("x", "t-1"),
        _final_response("done"),
    ])
    loop = _make_loop(llm=llm, plan_review=plan_review)
    await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[{"role": "user", "content": "go"}],
        goal_anchor_id="ga-x",
    )
    # 第 2 次 LLM call 的 messages 应该含 plan review prompt
    second_call_msgs = llm.calls[1]
    review_msgs = [
        m for m in second_call_msgs
        if "PLAN REVIEW" in str(m.get("content", ""))
    ]
    assert len(review_msgs) >= 1


@pytest.mark.asyncio
async def test_plan_review_not_injected_without_anchor() -> None:
    """没 goal_anchor_id 时 review 不调."""
    heartbeat = PlanReviewHeartbeat(step_interval=1, time_interval_sec=3600)
    plan_review = PlanReviewService(heartbeat=heartbeat)
    llm = _FakeLLM([
        _tool_response("x", "t-1"),
        _final_response("done"),
    ])
    loop = _make_loop(llm=llm, plan_review=plan_review)
    await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
        # goal_anchor_id 不传
    )
    for msgs in llm.calls:
        assert not any("PLAN REVIEW" in str(m.get("content", "")) for m in msgs)


# ---- Cost / tokens accumulated correctly ----


@pytest.mark.asyncio
async def test_cost_and_tokens_accumulate() -> None:
    llm = _FakeLLM([
        _tool_response("x", "t-1", cost=0.10, tokens=200),
        _tool_response("x", "t-2", cost=0.20, tokens=300),
        _final_response("done", cost=0.05, tokens=100),
    ])
    loop = _make_loop(llm=llm)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
    )
    assert result.total_cost_usd == pytest.approx(0.35)
    assert result.total_tokens == 600


# ---- elapsed_seconds populated ----


@pytest.mark.asyncio
async def test_elapsed_seconds_populated() -> None:
    llm = _FakeLLM([_final_response("done")])
    loop = _make_loop(llm=llm)
    result = await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=[],
    )
    assert result.elapsed_seconds >= 0.0
