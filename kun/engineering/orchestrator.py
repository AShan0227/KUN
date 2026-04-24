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
from typing import Any, cast

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from kun.brain.intent import IntentInterpreter
from kun.brain.planner import TaskPlanner
from kun.brain.router import TaskRouter
from kun.context.packer import ContextPacker
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.metrics import (
    task_duration_seconds,
    task_started_total,
    task_surprise_score,
)
from kun.core.orm import IdempotencyRow, RuntimeStateRow, TaskResultRow, TaskRow
from kun.core.tenancy import current_tenant
from kun.datamodel.events import Event
from kun.datamodel.notification import Notification
from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import Owner, TaskMeta, TaskRef
from kun.engineering.capability_writeback import Outcome, TaskOutcome, record_outcome
from kun.engineering.concurrency import (
    enqueue_pending_actions,
    pending_actions_for,
    scan_pre_conflicts,
)
from kun.engineering.validation import ValidationPipeline, pick_tier
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRouter,
    TaskProfile,
    get_router,
)
from kun.interface.llm.router import TaskPurpose
from kun.skills.selector import get_selector as get_skill_selector
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
        context_packer: ContextPacker | None = None,
    ) -> None:
        self.llm_router = llm_router or get_router()
        self.intent = IntentInterpreter(self.llm_router)
        self.planner = TaskPlanner()
        self.task_router = TaskRouter()
        self.rule_engine = rule_engine or RuleEngine()
        self.validation = validation or ValidationPipeline(self.llm_router)
        self.skill_selector = get_skill_selector()
        self.context_packer = context_packer or ContextPacker()

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

        # 2. Idempotency check + persist TaskRow + emit task.created
        duplicate_ref: str | None = None
        try:
            async with session_scope() as s:
                duplicate_ref = await _find_idempotent_result_ref(
                    s,
                    tenant_id=tenant.tenant_id,
                    fingerprint=task_ref.meta.fingerprint,
                )
                if duplicate_ref is None:
                    # Persist TaskRow and idempotency key in the same transaction.
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
                            spec_json=(
                                task_ref.spec.model_dump(mode="json") if task_ref.spec else None
                            ),
                            layer3_ref=task_ref.layer3_ref,
                        )
                    )
                    s.add(
                        IdempotencyRow(
                            key=task_ref.meta.fingerprint,
                            tenant_id=tenant.tenant_id,
                            result_ref=task_ref.meta.task_id,
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
                    await s.flush()
        except IntegrityError:
            # A concurrent request may have inserted the same tenant/fingerprint first.
            async with session_scope() as s:
                duplicate_ref = await _find_idempotent_result_ref(
                    s,
                    tenant_id=tenant.tenant_id,
                    fingerprint=task_ref.meta.fingerprint,
                )
            if duplicate_ref is None:
                raise

        if duplicate_ref is not None:
            cached_result = await _load_cached_task_result(
                tenant_id=tenant.tenant_id,
                task_id=duplicate_ref,
            )
            if cached_result is not None:
                yield OrchestratorEvent(
                    kind="insight",
                    data={
                        "message": "Duplicate task detected. Returning cached result.",
                        "cached_ref": duplicate_ref,
                        "status": cached_result.status,
                    },
                )
                yield OrchestratorEvent(
                    kind="answer",
                    data={"content": cached_result.answer, "task_id": duplicate_ref},
                )
                yield OrchestratorEvent(
                    kind="done",
                    data={"result": cached_result.model_dump(mode="json")},
                )
                return

            duplicate_status = await _load_task_status(duplicate_ref)
            duration = time.perf_counter() - t0
            message = f"Duplicate task detected. Existing task: {duplicate_ref}."
            result = TaskResult(
                task_id=duplicate_ref,
                status=duplicate_status,
                answer=message,
                duration_sec=duration,
            )
            yield OrchestratorEvent(
                kind="insight",
                data={
                    "message": message,
                    "cached_ref": duplicate_ref,
                    "status": duplicate_status,
                },
            )
            yield OrchestratorEvent(
                kind="answer",
                data={"content": message, "task_id": duplicate_ref},
            )
            yield OrchestratorEvent(
                kind="done",
                data={"result": result.model_dump(mode="json")},
            )
            return

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

        # 3. Planning
        plan = self.planner.plan(task_ref)

        # 4. Route (pick role + model purpose)
        choice = self.task_router.choose(task_ref.meta)

        # 5. Pre-start safety: conflict scan + pending side-effect actions.
        pending_actions = pending_actions_for(task_ref)
        async with session_scope() as s:
            pre_conflict_report = await scan_pre_conflicts(
                s,
                tenant_id=tenant.tenant_id,
                task_ref=task_ref,
            )
            if pending_actions:
                await enqueue_pending_actions(
                    s,
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    actions=pending_actions,
                )
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.pending_actions.created",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "actions": [
                                action.model_dump(mode="json") for action in pending_actions
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
            if pre_conflict_report.conflicts:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.pre_conflict_detected",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "conflicts": [
                                conflict.model_dump(mode="json")
                                for conflict in pre_conflict_report.conflicts
                            ],
                            "resources": [
                                resource.model_dump(mode="json")
                                for resource in pre_conflict_report.resources
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )

        if pre_conflict_report.blocking or pending_actions:
            reason_parts: list[str] = []
            if pre_conflict_report.blocking:
                resources = ", ".join(
                    sorted({conflict.resource for conflict in pre_conflict_report.conflicts})
                )
                reason_parts.append(f"检测到资源冲突: {resources}")
            if pending_actions:
                actions = ", ".join(action.action_type for action in pending_actions)
                reason_parts.append(f"检测到需要审批的外部副作用动作: {actions}")

            answer = "任务已暂停，等待确认。" + "；".join(reason_parts)
            paused_result = TaskResult(
                task_id=task_ref.meta.task_id,
                status="paused",
                answer=answer,
                duration_sec=time.perf_counter() - t0,
            )
            paused_runtime = RuntimeState(
                task_ref=task_ref.meta.task_id,
                total_planned_steps=len(plan.steps),
                status="paused",
                finished_at=datetime.now(UTC),
            )
            async with session_scope() as s:
                s.add(_runtime_to_row(paused_runtime, tenant.tenant_id))
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.paused.preflight",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "reason": answer,
                            "conflicts": [
                                conflict.model_dump(mode="json")
                                for conflict in pre_conflict_report.conflicts
                            ],
                            "pending_actions": [
                                action.model_dump(mode="json") for action in pending_actions
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
                await _persist_task_result(s, tenant_id=tenant.tenant_id, result=paused_result)

            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "preflight",
                    "reason": answer,
                    "conflicts": [
                        conflict.model_dump(mode="json")
                        for conflict in pre_conflict_report.conflicts
                    ],
                    "pending_actions": [
                        action.model_dump(mode="json") for action in pending_actions
                    ],
                },
            )
            yield OrchestratorEvent(
                kind="answer",
                data={"content": answer, "task_id": task_ref.meta.task_id},
            )
            yield OrchestratorEvent(
                kind="done",
                data={"result": paused_result.model_dump(mode="json")},
            )
            return

        # 6. Create RuntimeState
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

        # 7. Select candidate skills (L1 summary injected into step prompt)
        context_pack = await self.context_packer.pack(
            task_ref,
            tenant_id=tenant.tenant_id,
            limit=5,
        )
        context_summary = context_pack.summary()
        if context_pack.items:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "context_preheat",
                    "asset_ids": [item.asset_id for item in context_pack.items],
                },
            )

        skill_candidates = self.skill_selector.select(task_ref, top_k=3)
        if skill_candidates:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "skill_selection",
                    "candidates": [s.skill_id for s in skill_candidates],
                },
            )

        # 8. Execute steps
        answer = ""
        status: TaskStatus = "running"
        notifications: list[Notification] = []
        last_response: LLMResponse | None = None
        step_outputs: list[tuple[int, str]] = []

        try:
            for step_plan in plan.steps:
                step_t0 = time.perf_counter()
                yield OrchestratorEvent(
                    kind="action",
                    data={"step_id": step_plan.step_id, "description": step_plan.description},
                )

                # Execute via LLM with skill candidates hinted in system prompt
                answer, response = await self._execute_step(
                    task_ref=task_ref,
                    step_description=step_plan.description,
                    purpose=choice.purpose,
                    profile=choice.task_profile,
                    skills_summary=self.skill_selector.summary(skill_candidates),
                    context_summary=context_summary,
                    prior_outputs=step_outputs,
                )
                last_response = response
                step_outputs.append((step_plan.step_id, answer))

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

        async with session_scope() as s:
            await _persist_task_result(s, tenant_id=tenant.tenant_id, result=result)

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
        purpose: TaskPurpose,
        profile: TaskProfile,
        skills_summary: str = "",
        context_summary: str = "",
        prior_outputs: list[tuple[int, str]] | None = None,
    ) -> tuple[str, LLMResponse]:
        """Execute a single step by calling the LLM."""
        system_parts = [
            "你是 KUN 系统里的执行角色. 按用户要求完成任务, 回答简洁、准确、可验证. "
            "若需要外部数据, 说明需要什么. 不要编造."
        ]
        if skills_summary:
            system_parts.append(skills_summary)
        if context_summary:
            system_parts.append(context_summary)
        system_prompt = "\n\n".join(system_parts)
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=system_prompt, cache=True),
                LLMMessage(
                    role="user",
                    content=_execution_user_prompt(
                        task_ref,
                        step_description,
                        prior_outputs=prior_outputs or [],
                    ),
                ),
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


async def _find_idempotent_result_ref(
    session: Any,
    *,
    tenant_id: str,
    fingerprint: str,
) -> str | None:
    """Return the existing task id for a tenant/fingerprint, if any."""
    existing_key = await session.execute(
        select(IdempotencyRow).where(
            IdempotencyRow.key == fingerprint,
            IdempotencyRow.tenant_id == tenant_id,
        )
    )
    existing_row = existing_key.scalar_one_or_none()
    if existing_row is not None:
        return cast(str, existing_row.result_ref)

    existing_task = await session.execute(
        select(TaskRow.task_id).where(
            TaskRow.tenant_id == tenant_id,
            TaskRow.fingerprint == fingerprint,
        )
    )
    task_id = existing_task.scalar_one_or_none()
    return cast(str | None, task_id)


async def _load_cached_task_result(*, tenant_id: str, task_id: str) -> TaskResult | None:
    """Load a persisted final result for an idempotent duplicate request."""
    async with session_scope() as s:
        result = await s.execute(
            select(TaskResultRow.result_json).where(
                TaskResultRow.tenant_id == tenant_id,
                TaskResultRow.task_id == task_id,
            )
        )
        result_json = result.scalar_one_or_none()

    if isinstance(result_json, dict) and result_json:
        return TaskResult.model_validate(result_json)
    return None


async def _persist_task_result(session: Any, *, tenant_id: str, result: TaskResult) -> None:
    """Upsert the final task result so idempotent retries can return the old answer."""
    now = datetime.now(UTC)
    result_json = result.model_dump(mode="json")
    values = {
        "task_id": result.task_id,
        "tenant_id": tenant_id,
        "status": result.status,
        "answer": result.answer,
        "cost_usd_actual": result.cost_usd_actual,
        "cost_usd_equivalent": result.cost_usd_equivalent,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "duration_sec": result.duration_sec,
        "surprise_score": result.surprise_score,
        "notifications_json": [
            notification.model_dump(mode="json") for notification in result.notifications
        ],
        "result_json": result_json,
        "created_at": now,
        "updated_at": now,
    }
    stmt = pg_insert(TaskResultRow).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TaskResultRow.task_id],
            set_={
                "tenant_id": stmt.excluded.tenant_id,
                "status": stmt.excluded.status,
                "answer": stmt.excluded.answer,
                "cost_usd_actual": stmt.excluded.cost_usd_actual,
                "cost_usd_equivalent": stmt.excluded.cost_usd_equivalent,
                "tokens_in": stmt.excluded.tokens_in,
                "tokens_out": stmt.excluded.tokens_out,
                "duration_sec": stmt.excluded.duration_sec,
                "surprise_score": stmt.excluded.surprise_score,
                "notifications_json": stmt.excluded.notifications_json,
                "result_json": stmt.excluded.result_json,
                "updated_at": now,
            },
        )
    )


def _execution_user_prompt(
    task_ref: TaskRef,
    step_description: str,
    *,
    prior_outputs: list[tuple[int, str]] | None = None,
) -> str:
    """Build the execution prompt from TASK.md L1/L2 context."""
    lines = [
        "请执行当前任务步骤。",
        "",
        "任务身份:",
        f"- task_id: {task_ref.meta.task_id}",
        f"- task_type: {task_ref.meta.task_type}",
        f"- risk_level: {task_ref.meta.risk_level}",
        f"- complexity_score: {task_ref.meta.complexity_score:.2f}",
        "",
        "当前步骤:",
        f"- {step_description}",
        "",
        "成功标准:",
        f"- {task_ref.meta.success_criteria_short}",
    ]

    if prior_outputs:
        lines.extend(["", "已完成步骤输出摘要:"])
        for step_id, output in prior_outputs[-3:]:
            preview = output.strip().replace("\n", " ")
            if len(preview) > 400:
                preview = preview[:397].rstrip() + "..."
            lines.append(f"- step {step_id}: {preview}")

    if task_ref.spec is not None:
        spec = task_ref.spec
        lines.extend(["", "原始目标:", f"- {spec.goal_detail}"])
        if spec.success_metrics:
            lines.extend(["", "可验证指标:", *[f"- {metric}" for metric in spec.success_metrics]])
        if spec.constraints:
            lines.extend(
                [
                    "",
                    "约束:",
                    *[
                        f"- {constraint.kind}: {constraint.detail}"
                        for constraint in spec.constraints
                    ],
                ]
            )
        if spec.required_tools:
            lines.extend(["", "可能需要的工具:", *[f"- {tool}" for tool in spec.required_tools]])
        if spec.external_resources:
            lines.extend(
                ["", "外部资源:", *[f"- {resource}" for resource in spec.external_resources]]
            )
        if spec.foreseen_risks:
            lines.extend(
                [
                    "",
                    "已预见风险:",
                    *[
                        f"- {risk.severity}: {risk.description}"
                        + (f"；应对: {risk.mitigation_hint}" if risk.mitigation_hint else "")
                        for risk in spec.foreseen_risks
                    ],
                ]
            )
        if spec.fallback_plan:
            lines.extend(["", "失败回退方案:", f"- {spec.fallback_plan}"])

    lines.extend(
        [
            "",
            "输出要求:",
            "- 直接给结果。",
            "- 如果信息不足，明确说缺什么，不要编造。",
            "- 如果触碰约束或高风险动作，先说明风险和需要确认的点。",
        ]
    )
    return "\n".join(lines)


async def _load_task_status(task_id: str) -> TaskStatus:
    """Load the latest runtime status for an existing task."""
    async with session_scope() as s:
        result = await s.execute(
            select(RuntimeStateRow.status)
            .where(RuntimeStateRow.task_ref == task_id)
            .order_by(RuntimeStateRow.last_updated.desc())
            .limit(1)
        )
        status = result.scalar_one_or_none()

    if status in {"queued", "running", "paused", "done", "failed", "cancelled"}:
        return cast(TaskStatus, status)
    return "queued"


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
