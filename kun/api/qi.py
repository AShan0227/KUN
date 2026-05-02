"""V2.3 启 (Qi) HTTP API — status / start / stop / metrics.

跟 `kun qi status` CLI 同等功能, 让前端 web UI 显示启状态.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from kun.core.tenancy import current_tenant

router = APIRouter(prefix="/api/qi", tags=["qi"])


class QiStatusResponse(BaseModel):
    window_active: bool
    daily_limit_usd: float
    spent_today_usd: float
    remaining_usd: float
    qi_runtime_enabled: bool
    qi_force_active: bool
    protocol_count: int = 0
    pheromone_strength: float = 0.0
    problem_signal_count: int = 0
    top_problem: str = ""


class QiActionResponse(BaseModel):
    ok: bool
    message: str


def _qi_window_active(request: Request) -> bool:
    if os.getenv("KUN_QI_FORCE_DISABLE") == "1":
        return False
    if os.getenv("KUN_QI_FORCE_ACTIVE") == "1":
        return True
    qi_window = getattr(request.app.state, "qi_window_config", None)
    if qi_window is None:
        return False
    try:
        from kun.qi.window import is_qi_window_active

        return bool(is_qi_window_active(qi_window))
    except Exception:
        return False


@router.get("/status", response_model=QiStatusResponse)
async def qi_status(request: Request) -> QiStatusResponse:
    tenant = current_tenant()
    tenant_id = tenant.tenant_id

    budget = getattr(request.app.state, "qi_budget", None)
    if budget is not None:
        spent = budget.get_today_spent(tenant_id)
        remaining = budget.remaining_budget(tenant_id)
        daily_limit = budget._daily_limit
    else:
        spent = 0.0
        remaining = 0.0
        daily_limit = 0.0

    import contextlib

    # 协议数 (cheap, 不阻塞)
    proto_count = 0
    registry = getattr(request.app.state, "protocol_registry", None)
    if registry is not None:
        with contextlib.suppress(Exception):
            listed = await registry.list_all(tenant_id)
            proto_count = len(listed)

    # Pheromone 总强度
    pher_total = 0.0
    pher_storage = getattr(request.app.state, "pheromone_storage", None)
    if pher_storage is not None and hasattr(pher_storage, "_edges"):
        with contextlib.suppress(Exception):
            pher_total = float(
                sum(v for (t, *_), v in pher_storage._edges.items() if t == tenant_id)
            )

    problem_signal_count = 0
    top_problem = ""
    with contextlib.suppress(Exception):
        from kun.qi.problem_queue import get_configured_qi_problem_queue, get_qi_problem_queue

        queue = getattr(request.app.state, "qi_problem_queue", None)
        if queue is None:
            queue = get_configured_qi_problem_queue()
            request.app.state.qi_problem_queue = queue
        try:
            problems = await _queue_list(queue, tenant_id, limit=1)
            problem_signal_count = len(await _queue_list(queue, tenant_id, limit=1000))
        except Exception:
            fallback_queue = get_qi_problem_queue()
            request.app.state.qi_problem_queue = fallback_queue
            problems = fallback_queue.list(tenant_id, limit=1)
            problem_signal_count = len(fallback_queue.list(tenant_id, limit=1000))
        if problems:
            top_problem = problems[0].summary

    return QiStatusResponse(
        window_active=_qi_window_active(request),
        daily_limit_usd=daily_limit,
        spent_today_usd=spent,
        remaining_usd=remaining,
        qi_runtime_enabled=os.getenv("KUN_QI_RUNTIME_ENABLED", "1") == "1",
        qi_force_active=os.getenv("KUN_QI_FORCE_ACTIVE", "0") == "1",
        protocol_count=proto_count,
        pheromone_strength=pher_total,
        problem_signal_count=problem_signal_count,
        top_problem=top_problem,
    )


async def _queue_list(queue: Any, tenant_id: str, *, limit: int) -> list[Any]:
    listed = queue.list(tenant_id, limit=limit)
    if hasattr(listed, "__await__"):
        return list(await listed)
    return list(listed)


def _qi_problem_queue_db_enabled() -> bool:
    return os.getenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@router.post("/force_active", response_model=QiActionResponse)
async def qi_force_active(request: Request) -> QiActionResponse:
    """临时强制启窗口活跃 (process env, 重启失效).

    适合 dev / dogfood 场景, 不需等到 1-5 AM.
    """
    os.environ["KUN_QI_FORCE_ACTIVE"] = "1"
    os.environ.pop("KUN_QI_FORCE_DISABLE", None)
    return QiActionResponse(ok=True, message="启窗口已强制活跃 (此 process 内有效)")


@router.post("/release", response_model=QiActionResponse)
async def qi_release(request: Request) -> QiActionResponse:
    """释放强制活跃, 回到时间窗口判断."""
    os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
    return QiActionResponse(ok=True, message="启窗口强制活跃已释放, 回到正常窗口判断")


class TriggerExploreRequest(BaseModel):
    job: str = "darwin"  # darwin / ai_scientist / pc_train


@router.post("/trigger_explore", response_model=dict[str, Any])
async def qi_trigger_explore(payload: TriggerExploreRequest, request: Request) -> dict[str, Any]:
    """手动触发启窗口里的 cron job (Darwin / AI Scientist / PC train).

    用户在前端按"立即跑一次探索"时调. 不阻塞: 跑完返结果摘要.
    """
    tenant = current_tenant()
    tenant_id = tenant.tenant_id

    if payload.job == "darwin":
        from kun.qi.cron_jobs import _qi_darwin_godel_explore

        await _qi_darwin_godel_explore(request.app, tenant_id)
        return {
            "ok": True,
            "job": "darwin",
            "tenant": tenant_id,
            "note": "see backend logs for qi_darwin.done",
        }
    if payload.job == "ai_scientist":
        from kun.qi.cron_jobs import _qi_ai_scientist_explore

        await _qi_ai_scientist_explore(request.app, tenant_id)
        return {"ok": True, "job": "ai_scientist", "tenant": tenant_id}
    if payload.job == "pc_train":
        from kun.qi.cron_jobs import _qi_predictive_coding_train

        await _qi_predictive_coding_train(request.app, tenant_id)
        return {"ok": True, "job": "pc_train", "tenant": tenant_id}
    if payload.job == "auto_promote":
        from kun.qi.auto_promote import auto_promote_protocols

        result = await auto_promote_protocols(request.app, tenant_id)
        return {"ok": True, "job": "auto_promote", "tenant": tenant_id, **result}
    return {"ok": False, "error": f"unknown job: {payload.job}"}


@router.get("/learner/patterns", response_model=dict[str, Any])
async def qi_learner_patterns() -> dict[str, Any]:
    """V2.4: AntiGaming Learner — 用户负面反馈聚合的"可能新套路"."""
    tenant = current_tenant()
    from kun.qi.anti_gaming_learner import get_anti_gaming_learner

    learner = get_anti_gaming_learner()
    items = learner.top_patterns(tenant.tenant_id, limit=10)
    return {
        "tenant": tenant.tenant_id,
        "patterns": [
            {
                "pattern": p.pattern,
                "count": p.count,
                "examples": p.examples[:5],
                "first_seen": p.first_seen.isoformat(),
                "last_seen": p.last_seen.isoformat(),
            }
            for p in items
        ],
    }


@router.get("/verify/auto_template", response_model=dict[str, Any])
async def qi_verify_auto_template(task_type: str) -> dict[str, Any]:
    """V2.4: 给定 task_type 返自动生成的 verification template 建议."""
    from kun.qi.verification_auto_gen import get_verification_auto_gen

    gen = get_verification_auto_gen()
    template = gen.suggest(task_type)
    return {
        "task_type": template.task_type,
        "sample_size": template.sample_size,
        "suggested": template.suggested,
    }


@router.get("/windows", response_model=dict[str, Any])
async def qi_windows() -> dict[str, Any]:
    """V2.4: 列出当前所有启窗口 + 现在哪个活跃."""
    from kun.qi.multi_window import get_active_windows, is_any_window_active

    windows = get_active_windows()
    return {
        "any_active_now": is_any_window_active(),
        "windows": [
            {
                "start_hour": w.start_hour,
                "end_hour": w.end_hour,
                "weekdays": list(w.weekdays),
                "enabled": w.enabled,
            }
            for w in windows
        ],
    }


__all__ = ["router"]
