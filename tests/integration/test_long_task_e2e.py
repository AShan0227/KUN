"""LT.G — End-to-end integration test: 长复杂任务全栈联动.

把 LT.A-F 6 个 service 串成完整的 long-task runtime, 跑一个 simulated 长任务:

  1. Director 收用户 NL → IntentInterpreter 出 parsed dict + GoalAnchor
     (跳过 LLM 调用直接给 parsed 模拟)
  2. RecursivePlanner (LT.F) 把 task 拆 PlanTree (root + sub-steps + atomic leaves)
  3. ExecutorLoop (LT.E) 跑 multi-step loop:
     · Compactor (LT.D) 在 messages 超阈值时压缩
     · PlanReviewService (LT.B) 每 N step 注入 heartbeat prompt
     · TaskCheckpointService (LT.C) 每 step 落 checkpoint
  4. 跑到中途, 模拟用户发"我要换任务", LongTaskInputRouter (LT.A) → pivot_pause
  5. Resume: 新 ExecutorLoop 用 TaskCheckpointService.resume 拿到 checkpoint, 接续 sequence

零 LLM 实调 / 零 PG / 零 redis — 全 fake DI. 验证 6 个 service 都被调到 +
契约正确.
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.director.anchor import GoalAnchor
from kun.agents.director.long_task_router import LongTaskInputRouter
from kun.agents.director.recursive_planner import (
    PlanStepInput,
    PlanTree,
    RecursivePlanner,
)
from kun.agents.executor.checkpoint import (
    TaskCheckpoint,
    TaskCheckpointService,
)
from kun.agents.executor.compaction import ConversationCompactor
from kun.agents.executor.exec_loop import (
    ExecutorLoop,
    LLMStepResponse,
    ToolCall,
    ToolResult,
)
from kun.agents.supervisor.plan_review_heartbeat import PlanReviewHeartbeat
from kun.agents.supervisor.plan_review_service import PlanReviewService

# ============================================================
# Fakes for the e2e scenario
# ============================================================


class _InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.status_marks: list[tuple[str, str, str]] = []

    async def writer(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))

    async def reader(
        self, tenant_id: str, task_id: str
    ) -> TaskCheckpoint | None:
        actives = [
            r
            for r in self.rows
            if r["tenant_id"] == tenant_id
            and r["task_id"] == task_id
            and r["status"] == "active"
        ]
        if not actives:
            return None
        latest = max(actives, key=lambda r: r["sequence"])
        return TaskCheckpoint(
            checkpoint_id=latest["checkpoint_id"],
            task_id=latest["task_id"],
            step_idx=latest["step_idx"],
            sequence=latest["sequence"],
            conversation_snapshot=list(latest["conversation_snapshot"]),
            working_state=dict(latest["working_state"]),
            artifact_refs=list(latest["artifact_refs"]),
            goal_anchor_id=latest["goal_anchor_id"],
            last_self_report=latest["last_self_report"],
            cost_usd_so_far=latest["cost_usd_so_far"],
            tokens_used_so_far=latest["tokens_used_so_far"],
            status=latest["status"],
            rationale=latest["rationale"],
            created_at=latest["created_at"],
        )

    async def marker(
        self, tenant_id: str, checkpoint_id: str, new_status: str
    ) -> None:
        self.status_marks.append((tenant_id, checkpoint_id, new_status))
        for r in self.rows:
            if r["tenant_id"] == tenant_id and r["checkpoint_id"] == checkpoint_id:
                r["status"] = new_status


class _ScriptedLLM:
    """LLM 按预设脚本回放. tool 调用 → tool result → final answer."""

    def __init__(self, script: list[LLMStepResponse]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []
        self._idx = 0
        self.injected_review_prompts = 0

    async def __call__(
        self, messages: list[dict[str, Any]]
    ) -> LLMStepResponse:
        self.calls.append([dict(m) for m in messages])
        # Count plan_review prompts present in messages
        for m in messages:
            if "PLAN REVIEW" in str(m.get("content", "")):
                self.injected_review_prompts += 1
                break
        if self._idx >= len(self.script):
            # Fall through to last response if script exhausted
            return self.script[-1]
        r = self.script[self._idx]
        self._idx += 1
        return r


class _ScriptedTools:
    """Tool executor returning canned content. Records calls."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, calls: list[ToolCall]) -> list[ToolResult]:
        self.calls.extend(calls)
        return [
            ToolResult(
                tool_id=c.tool_id, content=f"ok:{c.name}({c.arguments})"
            )
            for c in calls
        ]


# ============================================================
# Helpers
# ============================================================


def _anchor() -> GoalAnchor:
    return GoalAnchor(
        task_id="tk-long-1",
        goal_statement="implement OAuth2 login with PKCE + session refresh",
        success_criteria=[
            "login endpoint returns 200 with valid PKCE flow",
            "refresh token rotates correctly",
            "tests pass with > 90% coverage",
        ],
        out_of_scope=["password reset", "social login providers"],
        invariants=["no plaintext secrets in logs"],
    )


def _final(text: str, *, cost: float = 0.01, tokens: int = 200) -> LLMStepResponse:
    return LLMStepResponse(
        content=text,
        tool_calls=[],
        finish_reason="end_turn",
        usage_tokens=tokens,
        cost_usd=cost,
    )


def _tool(
    name: str, tid: str, args: dict | None = None, *, cost: float = 0.005, tokens: int = 100
) -> LLMStepResponse:
    return LLMStepResponse(
        content="",
        tool_calls=[ToolCall(tool_id=tid, name=name, arguments=args or {})],
        finish_reason="tool_use",
        usage_tokens=tokens,
        cost_usd=cost,
    )


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_e2e_long_task_full_loop_with_all_services() -> None:
    """完整路径: anchor → tree → ExecutorLoop (LT.B/C/D 全集成) → final."""
    anchor = _anchor()

    # ---- LT.F: Build plan tree ----
    recursive = RecursivePlanner(max_depth=2)
    tree = await recursive.expand(
        root_description=anchor.goal_statement,
        root_steps=[
            PlanStepInput(
                description="add PKCE param to /login redirect",
                success_criterion="redirect contains code_challenge",
            ),
            PlanStepInput(
                description="implement refresh token rotation logic",
            ),
            PlanStepInput(
                description="add tests covering both flows",
                success_criterion="pytest passes for new test file",
            ),
        ],
    )
    assert isinstance(tree, PlanTree)
    assert tree.root().description == anchor.goal_statement
    assert tree.node_count() >= 4  # root + 3 steps (with possible sub-recursion)

    # ---- LT.B: Plan review service ----
    heartbeat = PlanReviewHeartbeat(step_interval=2, time_interval_sec=3600)
    plan_review = PlanReviewService(heartbeat=heartbeat)

    # ---- LT.C: Checkpoint service ----
    store = _InMemoryCheckpointStore()
    checkpoint = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )

    # ---- LT.D: Compactor ----
    compactor = ConversationCompactor(token_threshold=10_000, keep_last_k=4)

    # ---- LT.E: ExecutorLoop ----
    llm = _ScriptedLLM(
        [
            _tool("write_file", "t-1", {"path": "auth.py"}),
            _tool("run_tests", "t-2", {}),
            _tool("read_logs", "t-3", {}),
            _final("OAuth login implemented with PKCE; all tests pass"),
        ]
    )
    tools = _ScriptedTools()
    loop = ExecutorLoop(
        llm_invoker=llm,
        tool_executor=tools,
        max_steps=10,
        max_budget_usd=5.0,
        checkpoint_service=checkpoint,
        plan_review_service=plan_review,
        compactor=compactor,
    )

    result = await loop.run(
        task_id="tk-long-1",
        tenant_id="t-acme",
        initial_messages=[
            {"role": "system", "content": anchor.render_for_system_prompt()},
            {"role": "user", "content": "implement OAuth login"},
        ],
        goal_anchor_id=anchor.anchor_id,
        anchor_dict={"goal_statement": anchor.goal_statement},
    )

    # ---- Verify ----
    assert result.status == "final"
    assert "OAuth login" in result.final_text
    assert result.steps_taken == 4  # 3 tool steps + 1 final
    assert result.total_cost_usd == pytest.approx(0.015 + 0.01)  # 3 tool + 1 final
    assert result.last_checkpoint_id is not None

    # LT.C: checkpoints saved per step
    assert len(store.rows) >= 4
    final_rows = [r for r in store.rows if r["status"] == "final"]
    assert len(final_rows) == 1
    assert final_rows[0]["goal_anchor_id"] == anchor.anchor_id

    # LT.B: plan_review prompt injected at step 2 threshold
    # (step_interval=2 → second observe triggers)
    assert llm.injected_review_prompts >= 1

    # LT.C: anchor_id propagated to all checkpoints
    for r in store.rows:
        assert r["goal_anchor_id"] == anchor.anchor_id

    # LT.E: tool calls all dispatched
    assert len(tools.calls) == 3
    assert {c.name for c in tools.calls} == {"write_file", "run_tests", "read_logs"}


@pytest.mark.asyncio
async def test_e2e_resume_from_checkpoint_after_simulated_crash() -> None:
    """跑 2 step → 模拟进程挂 → 新 service 实例从 checkpoint resume 接续."""
    anchor = _anchor()
    store = _InMemoryCheckpointStore()

    # ---- Phase 1: 跑 2 step 然后假装挂 ----
    checkpoint_1 = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )
    llm_1 = _ScriptedLLM(
        [_tool("step1", "t-1"), _tool("step2", "t-2")]
    )
    loop_1 = ExecutorLoop(
        llm_invoker=llm_1,
        tool_executor=_ScriptedTools(),
        max_steps=2,  # 撞 max_steps 模拟挂掉
        checkpoint_service=checkpoint_1,
    )
    result_1 = await loop_1.run(
        task_id="tk-long-1",
        tenant_id="t-acme",
        initial_messages=[{"role": "user", "content": "go"}],
        goal_anchor_id=anchor.anchor_id,
    )
    assert result_1.status == "max_steps"
    cp_before = len(store.rows)
    assert cp_before >= 2

    # ---- Phase 2: 新 service 实例 resume ----
    checkpoint_2 = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )
    resumed_cp = await checkpoint_2.resume(
        tenant_id="t-acme",
        task_id="tk-long-1",
        current_anchor_id=anchor.anchor_id,
    )
    assert resumed_cp is not None
    assert resumed_cp.goal_anchor_id == anchor.anchor_id
    sequence_before_resume = resumed_cp.sequence

    # 下次 save 应该 sequence 接续 (sequence_before+1)
    new_cp = await checkpoint_2.save(
        tenant_id="t-acme",
        task_id="tk-long-1",
        step_idx=resumed_cp.step_idx + 1,
        conversation_snapshot=list(resumed_cp.conversation_snapshot),
        goal_anchor_id=anchor.anchor_id,
    )
    assert new_cp.sequence == sequence_before_resume + 1


@pytest.mark.asyncio
async def test_e2e_anchor_mismatch_on_resume_returns_none() -> None:
    """resume 时 anchor 变了 → 自动 failed_resume, caller 知道要重跑."""
    anchor_v1 = _anchor()
    store = _InMemoryCheckpointStore()
    checkpoint = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )
    # 第一次跑写 checkpoint with anchor_v1
    await checkpoint.save(
        tenant_id="t-acme",
        task_id="tk-long-1",
        step_idx=0,
        conversation_snapshot=[{"role": "user", "content": "..."}],
        goal_anchor_id=anchor_v1.anchor_id,
    )
    # 用户改 anchor (新 v2)
    anchor_v2_id = "ga-new-version"

    # Resume with new anchor → 应返 None + mark failed_resume
    new_checkpoint = TaskCheckpointService(
        writer=store.writer, reader=store.reader, status_marker=store.marker
    )
    resumed = await new_checkpoint.resume(
        tenant_id="t-acme",
        task_id="tk-long-1",
        current_anchor_id=anchor_v2_id,
    )
    assert resumed is None
    assert any(
        s[2] == "failed_resume" for s in store.status_marks
    )


@pytest.mark.asyncio
async def test_e2e_long_task_input_router_pivot_pauses() -> None:
    """LT.A: 长任务跑中, 用户发"换任务" → pivot_pause."""
    anchor = _anchor()
    pivot_events: list[tuple[str, str]] = []

    async def pivot_handler(task_id: str, raw_input: str) -> None:
        pivot_events.append((task_id, raw_input))

    router = LongTaskInputRouter(pivot_handler=pivot_handler)
    decision = await router.route(
        task_id="tk-long-1",
        new_input="我们换成做一个购物车页面吧",
        goal_anchor=anchor,
    )
    assert decision.bucket == "pivot_pause"
    assert decision.user_facing_reply is not None
    assert "确认切换" in decision.user_facing_reply
    assert pivot_events == [("tk-long-1", "我们换成做一个购物车页面吧")]


@pytest.mark.asyncio
async def test_e2e_long_task_off_topic_replies_without_polluting_loop() -> None:
    """LT.A: 长任务跑中, 用户闲聊 → off_topic, ExecutorLoop 不应看到."""
    anchor = _anchor()
    off_topic_replies: list[tuple[str, str]] = []

    async def off_topic_replier(task_id: str, reply: str) -> None:
        off_topic_replies.append((task_id, reply))

    router = LongTaskInputRouter(off_topic_replier=off_topic_replier)
    decision = await router.route(
        task_id="tk-long-1",
        new_input="今天天气不错",
        goal_anchor=anchor,
    )
    assert decision.bucket == "off_topic_reply"
    assert decision.user_facing_reply is not None
    assert len(off_topic_replies) == 1
    # 礼貌回复, 不影响 task working context
    assert "记下" in off_topic_replies[0][1]


@pytest.mark.asyncio
async def test_e2e_drift_detection_via_external_supervisor() -> None:
    """LT.B: External Supervisor 报警 → plan_review final verdict 升级."""

    async def harsh_external_verify(self_report, anchor_dict):
        # 模拟 External Supervisor 用独立模型说"看起来在偏"
        class FakeObs:
            verdict = "concerning"
            rationale = "recent steps haven't advanced any criterion"
        return FakeObs()

    heartbeat = PlanReviewHeartbeat()
    plan_review = PlanReviewService(
        heartbeat=heartbeat,
        external_supervisor_verify=harsh_external_verify,
    )

    outcome = await plan_review.submit_self_report(
        task_id="tk-long-1",
        anchor_id="ga-1",
        executor_self_report={
            "on_anchor": True,
            "scope_creep_detected": False,
            "recent_step_summary": "oauth login progress",
        },
        anchor_dict={"goal_statement": "implement oauth"},
    )
    # internal=aligned, external=drifting (concerning) → final=drifting (取严)
    assert outcome.internal_verdict == "aligned"
    assert outcome.external_verdict == "drifting"
    assert outcome.final_verdict == "drifting"
    assert outcome.action == "pause_for_anchor_recheck"


@pytest.mark.asyncio
async def test_e2e_compaction_triggers_under_threshold() -> None:
    """LT.D: ExecutorLoop 跑长对话, Compactor 应该触发."""
    compactor = ConversationCompactor(token_threshold=200, keep_last_k=2, protect_first_n=1)
    llm = _ScriptedLLM([_final("done")])
    loop = ExecutorLoop(
        llm_invoker=llm,
        tool_executor=_ScriptedTools(),
        compactor=compactor,
    )
    # 大量旧消息 → 触发 compaction
    initial = [{"role": "system", "content": "anchor"}] + [
        {"role": "user", "content": "x" * 400} for _ in range(10)
    ]
    await loop.run(
        task_id="tk-1",
        tenant_id="t-1",
        initial_messages=initial,
    )
    # 第一次 LLM call 看到的应该是 compacted messages (含 _kun_compacted summary)
    first_call_msgs = llm.calls[0]
    assert any(m.get("_kun_compacted") for m in first_call_msgs)


@pytest.mark.asyncio
async def test_e2e_recursive_plan_to_atomic_leaves() -> None:
    """LT.F: PlanTree leaves 都有 success_criterion (可独立验证)."""
    recursive = RecursivePlanner(max_depth=2)
    tree = await recursive.expand(
        root_description="implement OAuth login",
        root_steps=[
            PlanStepInput(description="will be recursed (no success_criterion)"),
            PlanStepInput(description="short", success_criterion="atomic by user"),
        ],
    )
    # All leaves are atomic + have success_criterion
    for leaf in tree.leaves():
        assert leaf.is_atomic is True
        assert leaf.success_criterion is not None
    # First step recursed; second step left atomic
    second_step = tree.children_of(tree.root_id)[1]
    assert second_step.success_criterion == "atomic by user"
