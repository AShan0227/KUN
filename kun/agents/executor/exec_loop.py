"""Multi-step execution loop (LT.E — long-task 真 agent loop).

Orchestrator 当前 one-shot LLM call. 此 ExecutorLoop 提供真 agent loop:

  while step < max_steps and budget_ok and not done:
    1. (LT.D) maybe_compact messages 超阈值时
    2. (LT.B) maybe inject plan_review prompt 心跳到点时
    3. invoke LLM
    4. 解 response:
       - tool_calls → dispatch tools, 拼 tool_results 回 messages
       - final answer → return with status='final'
    5. (LT.C) save checkpoint
    6. budget / wall-clock / stuck 检查

整合点:
  - TaskCheckpointService (LT.C) 每 step 后 save
  - PlanReviewService (LT.B) 每 step 后 observe + 拿 prompt
  - ConversationCompactor (LT.D) 每 step 前 maybe_compact
  - 全 DI: llm_invoker / tool_executor / 三个 service 都注入

设计原则:
  - 永远不 raise — 异常 → LoopResult(status='failed', error=str)
  - status='final' 表正常结束; 其他 status (max_steps / budget_exceeded /
    stuck / failed / user_cancelled) 都是异常退出
  - 永远落 checkpoint, 即使失败 — caller resume 时可看到状态
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kun.agents.executor.checkpoint import TaskCheckpointService
from kun.agents.executor.compaction import ConversationCompactor
from kun.agents.supervisor.plan_review_service import PlanReviewService
from kun.core.logging import get_logger

log = get_logger("kun.agents.executor.exec_loop")


# ---- Data types ----


@dataclass(frozen=True)
class ToolCall:
    """单次 LLM 输出的 tool call."""

    tool_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """单 tool 执行结果."""

    tool_id: str
    content: str  # tool 输出 (str 简化; 复杂 caller 自己 JSON 化)
    is_error: bool = False


@dataclass(frozen=True)
class LLMStepResponse:
    """LLM 单步返回 — provider-agnostic 抽象."""

    content: str  # text 部分
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "end_turn"  # tool_use / end_turn / max_tokens / stop
    usage_tokens: int = 0
    cost_usd: float = 0.0


LoopStatus = Literal[
    "final",  # LLM 给了 final answer (无 tool_calls)
    "max_steps",  # 撞 max_steps 上限
    "budget_exceeded",  # 撞 max_budget_usd 上限
    "wall_clock_exceeded",  # 撞 max_wall_seconds 上限
    "stuck",  # 连续 N 次 tool 失败 / 无进展
    "failed",  # llm_invoker 或 tool_executor 抛异常
    "user_cancelled",  # 调用方主动 cancel (e.g. LT.A interrupt)
]


@dataclass(frozen=True)
class LoopResult:
    """Loop 终态结果."""

    status: LoopStatus
    final_text: str
    steps_taken: int
    total_cost_usd: float
    total_tokens: int
    final_messages: list[dict[str, Any]]
    last_checkpoint_id: str | None
    elapsed_seconds: float
    rationale: str
    error: str | None = None


# ---- Callback types (all DI) ----

LLMInvoker = Callable[[list[dict[str, Any]]], Awaitable[LLMStepResponse]]
ToolExecutor = Callable[[list[ToolCall]], Awaitable[list[ToolResult]]]


# ---- Loop ----


class ExecutorLoop:
    """长任务 multi-step execution loop."""

    def __init__(
        self,
        *,
        llm_invoker: LLMInvoker,
        tool_executor: ToolExecutor,
        max_steps: int = 30,
        max_budget_usd: float = 1.0,
        max_wall_seconds: float = 1800.0,
        max_consecutive_tool_failures: int = 3,
        checkpoint_service: TaskCheckpointService | None = None,
        plan_review_service: PlanReviewService | None = None,
        compactor: ConversationCompactor | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if max_budget_usd <= 0:
            raise ValueError("max_budget_usd must be > 0")
        if max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be > 0")
        if max_consecutive_tool_failures < 1:
            raise ValueError("max_consecutive_tool_failures must be >= 1")
        self._llm = llm_invoker
        self._tools = tool_executor
        self._max_steps = max_steps
        self._max_budget = max_budget_usd
        self._max_wall = max_wall_seconds
        self._max_tool_fails = max_consecutive_tool_failures
        self._checkpoint = checkpoint_service
        self._plan_review = plan_review_service
        self._compactor = compactor

    async def run(
        self,
        *,
        task_id: str,
        tenant_id: str,
        initial_messages: list[dict[str, Any]],
        goal_anchor_id: str | None = None,
        anchor_dict: dict[str, Any] | None = None,
    ) -> LoopResult:
        """跑 loop. 永远返回 LoopResult, 不 raise."""
        started = time.perf_counter()
        messages: list[dict[str, Any]] = list(initial_messages)
        total_tokens = 0
        total_cost = 0.0
        steps = 0
        last_cp_id: str | None = None
        consecutive_tool_fails = 0
        final_text = ""

        while True:
            # ---- Termination checks ----
            if steps >= self._max_steps:
                return self._finalize(
                    status="max_steps",
                    final_text=final_text,
                    steps=steps,
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale=f"reached max_steps={self._max_steps}",
                )
            elapsed = time.perf_counter() - started
            if elapsed >= self._max_wall:
                return self._finalize(
                    status="wall_clock_exceeded",
                    final_text=final_text,
                    steps=steps,
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale=f"wall clock exceeded {self._max_wall}s",
                )
            if total_cost >= self._max_budget:
                return self._finalize(
                    status="budget_exceeded",
                    final_text=final_text,
                    steps=steps,
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale=f"budget ${self._max_budget} exceeded",
                )
            if consecutive_tool_fails >= self._max_tool_fails:
                return self._finalize(
                    status="stuck",
                    final_text=final_text,
                    steps=steps,
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale=f"{consecutive_tool_fails} consecutive tool failures",
                )

            # ---- (LT.D) Maybe compact ----
            if self._compactor is not None:
                try:
                    cresult = await self._compactor.maybe_compact(
                        messages, anchor=anchor_dict
                    )
                    if cresult is not None:
                        messages = list(cresult.compacted_messages)
                        log.info(
                            "exec_loop.compaction_applied",
                            task_id=task_id,
                            ratio=round(cresult.ratio, 3),
                            compacted=cresult.compacted_count,
                        )
                except Exception as e:
                    log.warning(
                        "exec_loop.compaction_failed",
                        task_id=task_id,
                        error=str(e),
                    )
                    # compaction 失败不阻塞主路径

            # ---- (LT.B) Maybe inject plan_review prompt ----
            if self._plan_review is not None and goal_anchor_id is not None:
                try:
                    review_prompt = await self._plan_review.observe_step_and_maybe_render_prompt(
                        task_id=task_id,
                        anchor_id=goal_anchor_id,
                    )
                    if review_prompt is not None:
                        messages.append({"role": "system", "content": review_prompt})
                        log.info(
                            "exec_loop.plan_review_injected",
                            task_id=task_id,
                            steps=steps,
                        )
                except Exception as e:
                    log.warning(
                        "exec_loop.plan_review_failed",
                        task_id=task_id,
                        error=str(e),
                    )

            # ---- LLM call ----
            try:
                response = await self._llm(messages)
            except Exception as e:
                return self._finalize(
                    status="failed",
                    final_text=final_text,
                    steps=steps,
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale=f"llm invocation failed: {e}",
                    error=str(e),
                )

            total_tokens += response.usage_tokens
            total_cost += response.cost_usd

            # Append assistant turn to messages
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }
            if response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "tool_id": tc.tool_id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in response.tool_calls
                ]
            messages.append(assistant_msg)

            # ---- final answer? ----
            if not response.tool_calls:
                final_text = response.content
                last_cp_id = await self._safe_save_checkpoint(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    step_idx=steps,
                    messages=messages,
                    goal_anchor_id=goal_anchor_id,
                    cost=total_cost,
                    tokens=total_tokens,
                    status="final",
                    rationale="final answer reached",
                )
                # finalize 在 service 层 (status_marker), 这里 save 时已经标 final
                return self._finalize(
                    status="final",
                    final_text=final_text,
                    steps=steps + 1,  # this step produced final
                    cost=total_cost,
                    tokens=total_tokens,
                    messages=messages,
                    last_cp_id=last_cp_id,
                    started=started,
                    rationale="LLM produced final answer (no tool_calls)",
                )

            # ---- Tool calls ----
            try:
                tool_results = await self._tools(response.tool_calls)
            except Exception as e:
                consecutive_tool_fails += 1
                log.warning(
                    "exec_loop.tool_executor_raised",
                    task_id=task_id,
                    error=str(e),
                    consecutive_fails=consecutive_tool_fails,
                )
                # fake "all errored" tool_results so loop continues normally
                tool_results = [
                    ToolResult(
                        tool_id=tc.tool_id,
                        content=f"tool_executor error: {e}",
                        is_error=True,
                    )
                    for tc in response.tool_calls
                ]

            # If all this round's tools errored, count as a failure
            if tool_results and all(r.is_error for r in tool_results):
                consecutive_tool_fails += 1
            else:
                consecutive_tool_fails = 0

            for r in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_id": r.tool_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                )

            # ---- (LT.C) Save checkpoint ----
            last_cp_id = await self._safe_save_checkpoint(
                task_id=task_id,
                tenant_id=tenant_id,
                step_idx=steps,
                messages=messages,
                goal_anchor_id=goal_anchor_id,
                cost=total_cost,
                tokens=total_tokens,
                status="active",
                rationale=f"after step {steps}",
            )

            steps += 1

    async def _safe_save_checkpoint(
        self,
        *,
        task_id: str,
        tenant_id: str,
        step_idx: int,
        messages: list[dict[str, Any]],
        goal_anchor_id: str | None,
        cost: float,
        tokens: int,
        status: str,
        rationale: str,
    ) -> str | None:
        """落 checkpoint, 失败不打挂 loop (但 log)."""
        if self._checkpoint is None:
            return None
        try:
            cp = await self._checkpoint.save(
                tenant_id=tenant_id,
                task_id=task_id,
                step_idx=step_idx,
                conversation_snapshot=messages,
                goal_anchor_id=goal_anchor_id,
                cost_usd_so_far=cost,
                tokens_used_so_far=tokens,
                status=status,  # type: ignore[arg-type]
                rationale=rationale,
            )
            return cp.checkpoint_id
        except Exception as e:
            log.warning(
                "exec_loop.checkpoint_save_failed",
                task_id=task_id,
                step_idx=step_idx,
                error=str(e),
            )
            return None

    def _finalize(
        self,
        *,
        status: LoopStatus,
        final_text: str,
        steps: int,
        cost: float,
        tokens: int,
        messages: list[dict[str, Any]],
        last_cp_id: str | None,
        started: float,
        rationale: str,
        error: str | None = None,
    ) -> LoopResult:
        return LoopResult(
            status=status,
            final_text=final_text,
            steps_taken=steps,
            total_cost_usd=cost,
            total_tokens=tokens,
            final_messages=list(messages),
            last_checkpoint_id=last_cp_id,
            elapsed_seconds=time.perf_counter() - started,
            rationale=rationale,
            error=error,
        )


__all__ = [
    "ExecutorLoop",
    "LLMInvoker",
    "LLMStepResponse",
    "LoopResult",
    "LoopStatus",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
]
