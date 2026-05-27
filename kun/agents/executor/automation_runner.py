"""AutomationRunner (L6.E) — Executor 侧 Adapter Router 调用方.

桥接 Phase 1 控制面 (Director / Executor) 和 Phase 2 自动化 (AdapterRouter):

  user NL → IntentInterpreter → parsed dict
            ↓
    extract_automation_action(parsed) → Action | None
            ↓ (if Action)
    AutomationRunner.run(action, router) → ActionResult
            ↓
        router.route_and_execute() → adapter.execute() → ActionResult

设计:
  - AutomationRunner 是个薄的 wrapper, 主要价值是:
    1. 提供清晰的 Executor 接口 (run_action)
    2. emit Phase 1 风格的 event (action.started / action.completed)
    3. 让单元测易写 (注入 router + emitter, 不依赖真 adapter)
  - 不引入新 dataclass — 直接用 Action / ActionResult / RouterDecision

  emit 失败语义 (ADR-024 frozen_dataclass_agent_io_contract 模式):
    - 状态累积 emit (start/end record) → try/except, log 不打挂主路径
    - Action 执行本身 raise → 上抛 (Executor 决策, 不吞)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kun.core.logging import get_logger
from kun.interface.automation import (
    Action,
    ActionResult,
    AdapterRouter,
    RouterDecision,
)

log = get_logger("kun.agents.executor.automation_runner")


ActionEventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Emit callback: (event_kind, payload_dict) → None. event_kind 例如
'action.started' / 'action.completed' / 'action.failed'."""


async def _noop_emitter(_kind: str, _payload: dict[str, Any]) -> None:
    return None


class AutomationRunner:
    """跑单个 Action 通过 AdapterRouter, emit 给 Phase 1 event 系统."""

    def __init__(
        self,
        router: AdapterRouter,
        *,
        emitter: ActionEventEmitter | None = None,
    ) -> None:
        self._router = router
        self._emitter = emitter or _noop_emitter

    async def run(self, action: Action) -> RouterDecision:
        """执行 action, emit 事件, 返回 RouterDecision (含 ActionResult).

        永远返回 RouterDecision — 即使 adapter 失败. status='failed' 在 result 里.
        只有真崩溃 (router 内部 bug / 找不到 adapter) 才 raise.
        """
        await self._safe_emit(
            "action.started",
            {
                "action_id": action.action_id,
                "target_platform": action.target_platform,
                "operation": action.operation,
                "tenant_id": action.tenant_id,
                "requested_kind": action.requested_kind,
            },
        )
        decision = await self._router.route_and_execute(action)
        result = decision.result
        if result is None:
            # Router 没找到 adapter — adapter_registry 缺这个 (platform, op)
            await self._safe_emit(
                "action.failed",
                {
                    "action_id": action.action_id,
                    "target_platform": action.target_platform,
                    "operation": action.operation,
                    "reason": "no_adapter_registered",
                    "decision": _decision_summary(decision),
                },
            )
            return decision

        event_kind = (
            "action.completed" if result.status == "ok" else "action.failed"
        )
        await self._safe_emit(
            event_kind,
            {
                "action_id": action.action_id,
                "target_platform": action.target_platform,
                "operation": action.operation,
                "status": result.status,
                "kind_used": result.kind_used,
                "latency_ms": result.latency_ms,
                "fallback_used": decision.fallback_used,
                "error": result.error,
            },
        )
        return decision

    async def _safe_emit(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            await self._emitter(kind, payload)
        except Exception as e:
            # 状态累积 emit 失败不打挂主路径 (ADR-024 模式)
            log.warning(
                "automation_runner.emit_failed",
                kind=kind,
                action_id=payload.get("action_id"),
                error=str(e),
            )


def _decision_summary(decision: RouterDecision) -> dict[str, Any]:
    """Decision 转 dict 给 event payload — 截断长字段."""
    sel = decision.selection
    return {
        "selected_kind": sel.kind,
        "selection_reason": sel.reason[:200] if sel.reason else None,
        "has_fallback_candidate": sel.fallback_candidate is not None,
        "fallback_used": decision.fallback_used,
        "rationale": decision.rationale[:200] if decision.rationale else None,
    }


def action_result_to_artifact(result: ActionResult) -> dict[str, Any]:
    """把 ActionResult 转成 Phase 1 风格的 artifact dict (供 capability writeback).

    供 Executor.run() 把 automation 结果落到 capability card.
    """
    return {
        "action_id": result.action_id,
        "status": result.status,
        "kind_used": result.kind_used,
        "result_payload": result.result_payload,
        "latency_ms": result.latency_ms,
        "rationale": result.rationale,
        "error": result.error,
        "artifact_refs": list(result.artifact_refs),
        "cost_usd": result.cost_usd,
    }


__all__ = [
    "ActionEventEmitter",
    "AutomationRunner",
    "action_result_to_artifact",
]
