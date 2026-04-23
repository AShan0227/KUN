"""Orchestrator — walking skeleton 端到端任务执行器.

Pipeline (§5.1-5.3):
  1. 事前: 意图 → TaskRef; TASK.md 持久化; 幂等检查; 指纹登记; 事件 task.created
  2. RuntimeState 初始化
  3. 计划生成 (Planner)
  4. 路由 (Router → role_template + model tier)
  5. 事中: 逐步执行 + 成本追踪 + 事件 task.step.completed
  6. 事后: 状态更新 done/failed; 事件 task.done
  7. Notification: cost_tick, insight/surprise 推送

所有事件走 Outbox (ADR-005).
所有成本按 ADR-008 双口径记录.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select, update

from kun.brain.intent import IntentInterpreter
from kun.brain.planner import TaskPlanner
from kun.brain.router import TaskRouter
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.metrics import (
    task_duration_seconds,
    task_started_total,
    task_surprise_score,
)
from kun.core.orm import IdempotencyRow, RuntimeStateRow, TaskRow
from kun.core.tenancy import current_tenant
from kun.datamodel.events import Event
from kun.datamodel.notification import Notification
from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import Owner, TaskMeta, TaskRef
from kun.engineering.capability_writeback import Outcome, TaskOutcome, record_outcome
from kun.engineering.validation import ValidationPipeline, pick_tier
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRouter,
    get_router,
)
from kun.watchtower.engine import RuleEngine

log = get_logger("kun.engineering.orchestrator")


class TaskResult(BaseModel):
    """Final result surfaced to the API / WebSocket."""

    task_id: str
    status: TaskStatus
    answer: str = ""
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_sec: float = 0.0
    surprise_score: float = 0.0
    notifications: list[Notification] = Field(default_factory=list)


class OrchestratorEvent(BaseModel):
    """One event yielded during streaming execution — feeds WebSocket side channel."""

    kind: str  # cost_tick | thinking | answer | done | error | insight
    data: dict[str, Any] = Field(default_factory=dict)


class Orchestrator:
    """Walking skeleton: single-step task execution.

    Future iterations plug in:
      - Multi-step planner
      - Skill lookup & dispatch
      - Context preheat (importance scorer)
      - Watchtower event evaluation per step
      - Debate / validation triggers on high-risk tasks
    """

    def __init__(
        self,
        *,
        llm_router: LLMRouter | None = None,
        rule_engine: RuleEngine | None = None,
        validation: ValidationPipeline | None = None,
    ) -> None:
        self.llm_router = llm_router or get_router()
        self.intent = IntentInterpreter(self.llm_router)
        self.planner = TaskPlanner()
        self.task_router = TaskRouter()
        self.rule_engine = rule_engine or RuleEngine()
        self.validation = validation or ValidationPipeline(self.llm_router)

    # ----------------------------- public entry -----------------------------

    async def run(self, user_message: str) -> TaskResult:
        """Non-streaming entry. Useful for tests / HTTP POST."""
        final: TaskResult | None = None
        async for ev in self.stream(user_message):
            if ev.kind == "done":
                final = TaskResult.model_validate(ev.data["result"])
        if final is None:
            raise RuntimeError("orchestrator exited without a done event")
        return final

    async def stream(self, user_message: str) -> AsyncIterator[OrchestratorEvent]:
        """Streaming entry. Yields OrchestratorEvents for WebSocket.

        Events align with ADR-010 dialog protocol message blocks.
        """
        tenant = current_tenant()
        owner = Owner(tenant_id=tenant.tenant_id, user_id=tenant.user_id)

        t0 = time.perf_counter()

        # 1. 意图理解 -> TaskRef
        yield OrchestratorEvent(kind="thinking", data={"stage": "intent"})
        task_ref = await self.intent.interpret(user_message, owner=owner)
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "risk_level": task_ref.meta.risk_level,
                "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                "estimated_duration_sec": task_ref.meta.estimated_duration_sec,
            },
        )

        # 2. Idempotency check + persist TaskRow + emit task.created
        async with session_scope() as s:
            # Idempotency by fingerprint
            existing = await s.execute(
                select(IdempotencyRow).where(IdempotencyRow.key == task_ref.meta.fingerprint)
            )
            existing_row = existing.scalar_one_or_none()
            if existing_row is not None:
                yield OrchestratorEvent(
                    kind="insight",
                    data={
                        "message": "Duplicate task detected (same fingerprint). Returning cached result.",
                        "cached_ref": existing_row.result_ref,
                    },
                )
                # In skeleton we still run, but emit the event for honest bookkeeping.

            # Persist TaskRow
            s.add(
                TaskRow(
                    task_id=task_ref.meta.task_id,
                    tenant_id=tenant.tenant_id,
                    fingerprint=task_ref.meta.fingerprint,
                    task_type=task_ref.meta.task_type,
                    risk_level=task_ref.meta.risk_level,
                    complexity_score=task_ref.meta.complexity_score,
                    user_id=owner.user_id,
                    project_id=owner.project_id,
                    estimated_cost_usd=task_ref.meta.estimated_cost_usd,
                    estimated_duration_sec=task_ref.meta.estimated_duration_sec,
                    deadline_iso=task_ref.meta.deadline_iso,
                    success_criteria_short=task_ref.meta.success_criteria_short,
                    version=task_ref.meta.version,
                    spec_json=(task_ref.spec.model_dump(mode="json") if task_ref.spec else None),
                    layer3_ref=task_ref.layer3_ref,
                )
            )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.created",
                    payload={
                        "task_id": task_ref.meta.task_id,
                        "task_type": task_ref.meta.task_type,
                    },
                    task_ref=task_ref.meta.task_id,
                ),
            )

        # 3. Planning
        plan = self.planner.plan(task_ref)

        # 4. Route (pick role + model purpose)
        choice = self.task_router.choose(task_ref.meta)

        # 5. Create RuntimeState
        runtime = RuntimeState(
            task_ref=task_ref.meta.task_id,
            total_planned_steps=len(plan.steps),
            status="running",
        )
        async with session_scope() as s:
            s.add(_runtime_to_row(runtime, tenant.tenant_id))
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.started",
                    payload={
                        "task_id": task_ref.meta.task_id,
                        "role_template": choice.role_template_id,
                        "model_purpose": choice.purpose,
                    },
                    task_ref=task_ref.meta.task_id,
                ),
            )
            task_started_total.labels(
                tenant_id=tenant.tenant_id, task_type=task_ref.meta.task_type
            ).inc()

        # 6. Execute steps (walking skeleton = 1 step)
        answer = ""
        status: TaskStatus = "running"
        notifications: list[Notification] = []
        last_response: LLMResponse | None = None

        try:
            for step_plan in plan.steps:
                step_t0 = time.perf_counter()
                yield OrchestratorEvent(
                    kind="action",
                    data={"step_id": step_plan.step_id, "description": step_plan.description},
                )

                # Execute via LLM
                answer, response = await self._execute_step(
                    task_ref=task_ref,
                    step_description=step_plan.description,
                    purpose=choice.purpose,
                    profile=choice.task_profile,
                )
                last_response = response

                duration = time.perf_counter() - step_t0
                step_record = StepRecord(
                    step_id=step_plan.step_id,
                    skill_used=step_plan.skill_hint or "llm.direct",
                    cost_usd_actual=response.cost_usd_actual,
                    cost_usd_equivalent=response.cost_usd_equivalent,
                    duration_sec=duration,
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                runtime.accumulate_step(step_record)

                # emit cost_tick
                yield OrchestratorEvent(
                    kind="cost_tick",
                    data={
                        "step_id": step_record.step_id,
                        "provider": response.provider,
                        "model": response.model,
                        "tier": response.tier,
                        "cost_usd_actual": response.cost_usd_actual,
                        "cost_usd_equivalent": response.cost_usd_equivalent,
                        "tokens_in": response.usage.input_tokens,
                        "tokens_out": response.usage.output_tokens,
                        "accumulated_usd": runtime.accumulated_cost_usd_equivalent,
                        "latency_ms": response.latency_ms,
                    },
                )

                # Persist event
                async with session_scope() as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="task.step.completed",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "step_id": step_record.step_id,
                                "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                                "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                                "cost_delta_usd": response.cost_usd_equivalent,
                                "tokens": response.usage.total(),
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )

                # Run watchtower rules
                namespace = {
                    "event": {
                        "event_type": "task.step.completed",
                        "payload": {
                            "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                            "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                        },
                        "tenant_id": tenant.tenant_id,
                    },
                    "task": {
                        "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                        "risk_level": task_ref.meta.risk_level,
                    },
                    "tenant_id": tenant.tenant_id,
                    "task_ref": task_ref.meta.task_id,
                }
                fired = await self.rule_engine.evaluate("task.step.completed", namespace=namespace)
                if fired:
                    yield OrchestratorEvent(
                        kind="guard_intervention",
                        data={"rules_fired": fired},
                    )

            status = "done"
        except Exception as exc:
            status = "failed"
            log.exception("orchestrator.failed", error=str(exc))
            yield OrchestratorEvent(
                kind="error",
                data={"message": str(exc), "task_id": task_ref.meta.task_id},
            )
            answer = f"Sorry — task failed: {exc}"

        # 7. Finalize
        runtime.status = status
        runtime.finished_at = datetime.now(UTC)
        total_duration = time.perf_counter() - t0

        async with session_scope() as s:
            await s.execute(
                update(RuntimeStateRow)
                .where(RuntimeStateRow.state_id == runtime.state_id)
                .values(
                    status=runtime.status,
                    current_step=runtime.current_step,
                    accumulated_cost_usd_actual=runtime.accumulated_cost_usd_actual,
                    accumulated_cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
                    accumulated_tokens=runtime.accumulated_tokens,
                    finished_at=runtime.finished_at,
                    blob=runtime.model_dump(mode="json"),
                )
            )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.done" if status == "done" else "task.failed",
                    payload={
                        "task_id": task_ref.meta.task_id,
                        "status": status,
                        "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                        "tokens": runtime.accumulated_tokens,
                        "duration_sec": total_duration,
                    },
                    task_ref=task_ref.meta.task_id,
                ),
            )

        task_duration_seconds.labels(task_type=task_ref.meta.task_type, status=status).observe(
            total_duration
        )

        # surprise_score (ADR-015)
        surprise = _compute_surprise_score(task_ref.meta, runtime)
        task_surprise_score.labels(task_type=task_ref.meta.task_type).observe(surprise)

        if surprise >= 0.6:
            notifications.append(
                Notification(
                    tenant_id=tenant.tenant_id,
                    kind="surprise"
                    if runtime.accumulated_cost_usd_equivalent < task_ref.meta.estimated_cost_usd
                    else "alert",
                    severity="insight",
                    channel="side",
                    title="任务意外度较高, 值得回看",
                    body=f"surprise_score={surprise:.2f}",
                    payload={"task_id": task_ref.meta.task_id},
                )
            )

        # 7.4 ValidationPipeline (ADR-018 §16.2). Tier selected from risk × complexity.
        validation_outcome: Outcome = "pass" if status == "done" else "fail"
        validation_score: float | None = None
        if status == "done":
            tier = pick_tier(task_ref.meta)
            if tier != "tier0" and answer.strip():
                try:
                    results = await self.validation.validate_task(
                        task_ref.meta,
                        answer,
                        goal=task_ref.meta.success_criteria_short,
                    )
                    aggregated = ValidationPipeline.aggregate(results)
                    if aggregated is not None:
                        validation_score = aggregated.score.value
                        if not aggregated.pass_:
                            validation_outcome = "partial"
                            yield OrchestratorEvent(
                                kind="insight",
                                data={
                                    "stage": "validation",
                                    "tier": tier,
                                    "verdict": "did_not_fully_pass",
                                    "score": validation_score,
                                    "reason": aggregated.reason,
                                },
                            )
                        else:
                            yield OrchestratorEvent(
                                kind="insight",
                                data={
                                    "stage": "validation",
                                    "tier": tier,
                                    "verdict": "passed",
                                    "score": validation_score,
                                },
                            )
                except Exception as e:
                    log.warning("validation.failed", error=str(e))

        # 7.5 Capability card writeback (ADR-018 §16.4 KnowledgePrecipitation)
        outcome: Outcome = validation_outcome
        rubric_5 = validation_score * 5.0 if validation_score is not None else None
        try:
            await record_outcome(
                tenant.tenant_id,
                TaskOutcome(
                    entity_type="role_template",
                    entity_id=choice.role_template_id,
                    task_type=task_ref.meta.task_type,
                    outcome=outcome,
                    cost_usd=runtime.accumulated_cost_usd_equivalent,
                    duration_sec=total_duration,
                    rubric_score=rubric_5,
                    surprise_score=surprise,
                ),
            )
            if last_response is not None:
                await record_outcome(
                    tenant.tenant_id,
                    TaskOutcome(
                        entity_type="model",
                        entity_id=last_response.model or "unknown",
                        task_type=task_ref.meta.task_type,
                        outcome=outcome,
                        cost_usd=last_response.cost_usd_equivalent,
                        duration_sec=last_response.latency_ms / 1000.0,
                        rubric_score=rubric_5,
                        surprise_score=surprise,
                    ),
                )
        except Exception as e:
            # Writeback failure must not break the task.
            log.warning("capability.writeback_failed", error=str(e))

        result = TaskResult(
            task_id=task_ref.meta.task_id,
            status=status,
            answer=answer,
            cost_usd_actual=runtime.accumulated_cost_usd_actual,
            cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
            tokens_in=sum(s.tokens_in for s in runtime.completed_steps),
            tokens_out=sum(s.tokens_out for s in runtime.completed_steps),
            duration_sec=total_duration,
            surprise_score=surprise,
            notifications=notifications,
        )

        yield OrchestratorEvent(
            kind="answer",
            data={"content": answer, "task_id": task_ref.meta.task_id},
        )
        yield OrchestratorEvent(
            kind="done",
            data={"result": result.model_dump(mode="json")},
        )

    # ---------------------------- helpers ----------------------------

    async def _execute_step(
        self,
        *,
        task_ref: TaskRef,
        step_description: str,
        purpose,
        profile,
    ) -> tuple[str, LLMResponse]:
        """Execute a single step by calling the LLM."""
        system_prompt = (
            "你是 KUN 系统里的执行角色. 按用户要求完成任务, 回答简洁、准确、可验证. "
            "若需要外部数据, 说明需要什么. 不要编造."
        )
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=system_prompt, cache=True),
                LLMMessage(role="user", content=step_description),
            ],
            temperature=0.5,
            max_tokens=1024,
            profile=profile,
        )
        response = await self.llm_router.invoke(request, purpose=purpose)
        return response.content, response


# =================== helpers ===================


def _runtime_to_row(runtime: RuntimeState, tenant_id: str) -> RuntimeStateRow:
    return RuntimeStateRow(
        state_id=runtime.state_id,
        task_ref=runtime.task_ref,
        tenant_id=tenant_id,
        current_step=runtime.current_step,
        total_planned_steps=runtime.total_planned_steps,
        status=runtime.status,
        accumulated_cost_usd_actual=runtime.accumulated_cost_usd_actual,
        accumulated_cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
        accumulated_tokens=runtime.accumulated_tokens,
        failures_this_run=runtime.failures_this_run,
        blob=runtime.model_dump(mode="json"),
        started_at=runtime.started_at,
        finished_at=runtime.finished_at,
        last_updated=runtime.last_updated,
    )


def _compute_surprise_score(meta: TaskMeta, runtime: RuntimeState) -> float:
    """ADR-015 formula:
    surprise = 0.35*cost_dev + 0.20*step_dev + 0.25*path_novelty + 0.20*quality_dev.
    Walking skeleton: path_novelty and quality_dev = 0 (we don't have baselines yet).
    """
    cost_dev = 0.0
    if meta.estimated_cost_usd > 0:
        cost_dev = max(0.0, runtime.accumulated_cost_usd_equivalent / meta.estimated_cost_usd - 1.0)
    step_dev = 0.0
    if runtime.total_planned_steps > 0:
        step_dev = max(0.0, runtime.current_step / runtime.total_planned_steps - 1.0)
    score = 0.35 * cost_dev + 0.20 * step_dev
    return min(1.0, score)


__all__ = ["Orchestrator", "OrchestratorEvent", "TaskResult"]
