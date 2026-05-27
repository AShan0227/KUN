"""PlanReviewService — Layer 4 high-level wiring (LT.B, ADR-022).

把已有的 PlanReviewHeartbeat (counter + 规则评判) 接到完整 runtime:
  1. Executor 完成一步 → service.on_step_completed_observe(...)
  2. heartbeat 判定该复审 → service render prompt → caller pin 进下次 LLM call
  3. Executor 输出 self_report JSON → service.submit_self_report(...)
  4. service 调 ExternalSupervisorService.analyze_observation 独立 verify
  5. 合并 internal + external verdict (取更严的) → PlanReviewOutcome
  6. caller 按 outcome.action 决定 continue / pause / RCDH

设计原则:
  - 内外 verdict 取严: 任一 'off_track' 则 final='off_track'
  - External Supervisor 不可用 (未配置 / 调用 raise) → 仅用 internal, 不阻塞主路径
  - 全部 callback 注入: 测试用 fake_external_supervisor, prod 用真 LLM
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from kun.agents.supervisor.plan_review_heartbeat import (
    PlanReviewHeartbeat,
    ReviewTrigger,
    derive_action,
    evaluate_executor_self_report,
)
from kun.agents.supervisor.plan_review_prompt import render_plan_review_prompt
from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.plan_review_service")


VerdictName = Literal["aligned", "drifting", "off_track"]


@dataclass(frozen=True)
class PlanReviewOutcome:
    """单次 plan_review 的完整结果."""

    triggered: bool
    trigger: ReviewTrigger | None
    internal_verdict: VerdictName | None
    external_verdict: VerdictName | None  # None 表 External Supervisor 未启用 / 失败
    final_verdict: VerdictName
    drift_evidence: list[dict[str, Any]]
    action: str
    """continue / pause_for_anchor_recheck / trigger_rcdh_level_0"""
    rationale: str
    review_prompt: str | None = None
    """供 caller 拼入下次 LLM call system prompt 的 review prompt 段"""


# ExternalSupervisorVerify is what we expect — accept anything callable returning
# an object with `.verdict` (str: ok/concerning/alarming) and `.rationale`.
ExternalSupervisorVerify = Callable[
    [dict[str, Any], dict[str, Any] | None], Awaitable[Any]
]
"""(self_report_dict, anchor_dict_or_none) → ExternalSupervisorObservation-like.

Result.verdict in {ok, concerning, alarming}.
"""


# Map External Supervisor verdict → internal verdict scale
_EXTERNAL_TO_INTERNAL: dict[str, VerdictName] = {
    "ok": "aligned",
    "concerning": "drifting",
    "alarming": "off_track",
}

_VERDICT_SEVERITY: dict[VerdictName, int] = {
    "aligned": 0,
    "drifting": 1,
    "off_track": 2,
}


def _combine_verdicts(
    internal: VerdictName, external: VerdictName | None
) -> VerdictName:
    """取更严的 verdict (defense-in-depth)."""
    if external is None:
        return internal
    if _VERDICT_SEVERITY[external] > _VERDICT_SEVERITY[internal]:
        return external
    return internal


class PlanReviewService:
    """长任务 Plan Review Service — runtime wiring of Layer 4."""

    def __init__(
        self,
        *,
        heartbeat: PlanReviewHeartbeat,
        external_supervisor_verify: ExternalSupervisorVerify | None = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._external_verify = external_supervisor_verify

    async def observe_step_and_maybe_render_prompt(
        self,
        *,
        task_id: str,
        anchor_id: str,
        executor_self_report: dict[str, Any] | None = None,
        goal_tokens: set[str] | None = None,
        recent_steps: list[str] | None = None,
    ) -> str | None:
        """Executor 完成一步 → 通知 heartbeat. 若到阈值 → 返回要拼入下次
        LLM call 的 review prompt; 否则 None.

        当 executor_self_report 为 None 时 (第一次 step 还没自评数据), 用
        空 dict 让 heartbeat 仅评 trigger 阈值 (verdict 必为 aligned, 但
        prompt 仍会 render — 让下次 LLM call 给真自评).
        """
        trigger = await self._heartbeat.on_step_completed(
            task_id=task_id,
            anchor_id=anchor_id,
            executor_self_report=executor_self_report or {},
            goal_tokens=goal_tokens,
        )
        if trigger is None:
            return None
        return render_plan_review_prompt(
            reason=trigger.reason,
            recent_steps=recent_steps,
            extras={
                "total_steps": trigger.payload.get("total_steps"),
                "total_reviews": trigger.payload.get("total_reviews"),
            },
        )

    async def submit_self_report(
        self,
        *,
        task_id: str,
        anchor_id: str,
        executor_self_report: dict[str, Any],
        goal_tokens: set[str] | None = None,
        anchor_dict: dict[str, Any] | None = None,
    ) -> PlanReviewOutcome:
        """处理 Executor 给出的 self_report → internal + external verdict
        → 决定 action.

        不依赖 heartbeat (这步在 prompt 注入后单独跑) — 但仍写 internal 评判.
        caller 应该已经从 heartbeat 拿到 trigger; 这里没 trigger 入参时填空.
        """
        internal_verdict, drift_evidence = evaluate_executor_self_report(
            executor_self_report, goal_tokens=goal_tokens
        )
        external_verdict: VerdictName | None = None
        external_rationale = ""

        if self._external_verify is not None:
            try:
                observation = await self._external_verify(
                    executor_self_report, anchor_dict
                )
                raw_external = str(getattr(observation, "verdict", "")).lower()
                external_verdict = _EXTERNAL_TO_INTERNAL.get(raw_external)
                external_rationale = str(getattr(observation, "rationale", ""))
                log.info(
                    "plan_review_service.external_verified",
                    task_id=task_id,
                    raw_verdict=raw_external,
                    mapped=external_verdict,
                )
            except Exception as e:
                log.warning(
                    "plan_review_service.external_verify_failed",
                    task_id=task_id,
                    error=str(e),
                )

        final = _combine_verdicts(internal_verdict, external_verdict)  # type: ignore[arg-type]
        action = derive_action(final)
        rationale_parts = [f"internal={internal_verdict}"]
        if external_verdict is not None:
            rationale_parts.append(f"external={external_verdict}")
        if external_rationale:
            rationale_parts.append(f"external_says='{external_rationale[:120]}'")
        rationale_parts.append(f"final={final} → action={action}")
        rationale = "; ".join(rationale_parts)

        return PlanReviewOutcome(
            triggered=True,
            trigger=None,
            internal_verdict=internal_verdict,  # type: ignore[arg-type]
            external_verdict=external_verdict,
            final_verdict=final,
            drift_evidence=drift_evidence,
            action=action,
            rationale=rationale,
        )


__all__ = [
    "ExternalSupervisorVerify",
    "PlanReviewOutcome",
    "PlanReviewService",
    "VerdictName",
]
