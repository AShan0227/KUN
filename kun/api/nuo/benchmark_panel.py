"""傩 · 外部 agent benchmark 面板。"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant
from kun.engineering.agent_benchmark import (
    AgentBenchmarkResult,
    BenchmarkTask,
    aggregate_results,
    run_benchmark,
    sample_benchmark_tasks,
)

router = APIRouter()
AgentInvoke = Callable[[str], Awaitable[str]]


class BenchmarkTaskIn(BaseModel):
    task_id: str
    task_type: str
    prompt: str
    expected_kind: Literal["code_compile", "exact_match", "rubric_score"]
    expected: Any


class BenchmarkRunRequest(BaseModel):
    agent_ref: str
    tasks: list[BenchmarkTaskIn] | None = None


class BenchmarkRunRecord(BaseModel):
    run_id: str
    tenant_id: str
    agent_ref: str
    started_at: datetime
    finished_at: datetime
    summary: dict[str, float] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)


class BenchmarkAgentSummary(BaseModel):
    agent_ref: str
    latest_run_id: str | None = None
    latest_summary: dict[str, float] = Field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkAgentRegistration:
    tenant_id: str
    agent_ref: str
    invoke: AgentInvoke


_AGENTS: dict[tuple[str, str], BenchmarkAgentRegistration] = {}
_RUNS: dict[str, BenchmarkRunRecord] = {}


def register_agent(agent_ref: str, invoke: AgentInvoke) -> None:
    """注册一个可 benchmark 的 agent。"""
    tenant_id = current_tenant().tenant_id
    _AGENTS[(tenant_id, agent_ref)] = BenchmarkAgentRegistration(
        tenant_id=tenant_id,
        agent_ref=agent_ref,
        invoke=invoke,
    )


def clear_benchmark_state() -> None:
    """测试用：清掉内存注册表和结果。"""
    _AGENTS.clear()
    _RUNS.clear()


@router.get("/agents", response_model=list[BenchmarkAgentSummary])
async def list_benchmark_agents() -> list[BenchmarkAgentSummary]:
    """列出已注册 agent 和最新得分。"""
    tenant_id = current_tenant().tenant_id
    summaries: list[BenchmarkAgentSummary] = []
    registrations = sorted(
        (agent for agent in _AGENTS.values() if agent.tenant_id == tenant_id),
        key=lambda agent: agent.agent_ref,
    )
    for registration in registrations:
        latest = _latest_run_for(agent_ref=registration.agent_ref, tenant_id=tenant_id)
        summaries.append(
            BenchmarkAgentSummary(
                agent_ref=registration.agent_ref,
                latest_run_id=latest.run_id if latest else None,
                latest_summary=latest.summary if latest else {},
            )
        )
    return summaries


@router.post("/run", response_model=BenchmarkRunRecord)
async def start_benchmark_run(req: BenchmarkRunRequest) -> BenchmarkRunRecord:
    """启动一轮 benchmark。当前版本同步执行，后续可接后台任务。"""
    tenant = current_tenant()
    registration = _AGENTS.get((tenant.tenant_id, req.agent_ref))
    if registration is None:
        raise HTTPException(
            status_code=404, detail=f"benchmark agent not registered: {req.agent_ref}"
        )

    tasks = (
        [_task_from_input(task) for task in req.tasks] if req.tasks else sample_benchmark_tasks()
    )
    started = datetime.now(UTC)
    results = await run_benchmark(
        agent_invoke=registration.invoke,
        tasks=tasks,
        agent_ref=req.agent_ref,
    )
    finished = datetime.now(UTC)
    run = BenchmarkRunRecord(
        run_id=f"bench-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant.tenant_id,
        agent_ref=req.agent_ref,
        started_at=started,
        finished_at=finished,
        summary=aggregate_results(results),
        results=[_result_to_dict(result) for result in results],
    )
    _RUNS[run.run_id] = run
    return run


@router.get("/results/{run_id}", response_model=BenchmarkRunRecord)
async def get_benchmark_result(run_id: str) -> BenchmarkRunRecord:
    tenant_id = current_tenant().tenant_id
    run = _RUNS.get(run_id)
    if run is None or run.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="benchmark run not found")
    return run


def _latest_run_for(*, agent_ref: str, tenant_id: str) -> BenchmarkRunRecord | None:
    runs = [
        run for run in _RUNS.values() if run.agent_ref == agent_ref and run.tenant_id == tenant_id
    ]
    if not runs:
        return None
    return max(runs, key=lambda run: run.finished_at)


def _task_from_input(task: BenchmarkTaskIn) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task.task_id,
        task_type=task.task_type,
        prompt=task.prompt,
        expected_kind=task.expected_kind,
        expected=task.expected,
    )


def _result_to_dict(result: AgentBenchmarkResult) -> dict[str, Any]:
    return {
        "agent_ref": result.agent_ref,
        "task_id": result.task_id,
        "success": result.success,
        "score": result.score,
        "cost_usd": result.cost_usd,
        "duration_sec": result.duration_sec,
    }


__all__ = ["clear_benchmark_state", "register_agent", "router"]
