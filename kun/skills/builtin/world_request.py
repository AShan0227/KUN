"""world-request skill — ask KUN to pause and queue a WorldGateway action.

This skill is deliberately not an executor. It only packages a side-effect
request so the orchestrator can persist it as a pending action and pause for
NUO/human approval. Real dispatch still goes through WorldGateway + approval
executor.
"""

from __future__ import annotations

import time
from typing import Any

from kun.engineering.concurrency import PendingActionSpec
from kun.skills.dispatcher import SkillResult, register


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    context = params.get("_kun_context")
    if not isinstance(context, dict):
        context = {}

    task_id = str(params.get("task_id") or context.get("task_id") or "").strip()
    tenant_id = str(params.get("tenant_id") or context.get("tenant_id") or "").strip()
    action_type = str(params.get("action_type") or "").strip()
    target_ref = str(params.get("target_ref") or params.get("target") or "unknown").strip()
    risk_level = str(params.get("risk_level") or context.get("risk_level") or "medium").strip()
    raw_payload = params.get("payload") or {}
    if not isinstance(raw_payload, dict):
        return SkillResult(
            skill_id="world-request",
            ok=False,
            error="payload must be an object",
            duration_sec=time.perf_counter() - started,
        )
    if not task_id:
        return SkillResult(
            skill_id="world-request",
            ok=False,
            error="task_id is required in context",
            duration_sec=time.perf_counter() - started,
        )
    if not action_type:
        return SkillResult(
            skill_id="world-request",
            ok=False,
            error="action_type is required",
            duration_sec=time.perf_counter() - started,
        )

    payload = {
        **raw_payload,
        "source": "world-request skill",
        "requested_by": "llm_execution_loop",
        "tenant_id": tenant_id,
        "task_id": task_id,
    }
    action = PendingActionSpec(
        action_type=action_type,
        target_ref=target_ref or "unknown",
        risk_level=risk_level,
        payload=payload,
    )
    action_json = action.model_dump(mode="json")
    return SkillResult(
        skill_id="world-request",
        ok=True,
        output={
            "status": "pending_approval_requested",
            "message": "外部动作已转成待审批请求。任务应暂停，等待 NUO/用户确认。",
            "pending_action": action_json,
        },
        duration_sec=time.perf_counter() - started,
        metadata={
            "requires_task_pause": True,
            "pending_actions": [action_json],
            "pause_reason": "world_action_requires_approval",
        },
    )


register("world-request", execute)
