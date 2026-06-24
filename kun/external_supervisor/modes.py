"""External Supervisor 三种监督模式 (ADR-023 §Modes).

  Mode A · gate_review        — Gate / 高风险决策前同步复核 (Director 调)
  Mode B · task_debrief        — 任务尾复盘, 写 evidence_ledger
  Always · self_aggrandizement_check — 自嗨 / 假通过检测, 每次任务必跑

每个 Mode = 工程化检查 (规则 + 启发式) → LLM 独立 verdict → 综合 → 结构化输出.
工程化优先, LLM 兜底; LLM 异常时仅退到工程化结果.

L2.5 不接 NATS 订阅 (那是 L3 闭环扩散). L2.5 提供 sync API, 让主线直接调.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kun.external_supervisor.service import (
    ExternalSupervisorObservation,
    ExternalSupervisorService,
)

# ---- 结构化输出类型 ----


@dataclass(frozen=True)
class GateAdvisory:
    """Mode A 输出 — Gate sync 复核结果."""

    advisory_id: str
    verdict: str  # approve / block / escalate
    rationale: str
    recommended_action: str  # continue / pause / cancel / human_review
    underlying: ExternalSupervisorObservation


@dataclass(frozen=True)
class DebriefRecord:
    """Mode B 输出 — task done 后复盘记录, 进 evidence_ledger."""

    debrief_id: str
    verdict: str  # ok / concerning / alarming
    rationale: str
    evidence_quality_score: float  # 0.0-1.0, 越高证据越充分
    recommended_capability_writeback: dict[str, Any]
    underlying: ExternalSupervisorObservation
    triggered_at: datetime


@dataclass(frozen=True)
class SelfAggrandizementCheck:
    """自嗨检测结果. 每次任务必跑, 防主线 LLM 自报"全部通过"."""

    check_id: str
    is_self_aggrandizing: bool
    engineering_signals: list[str]  # 工程化规则命中
    llm_verdict: str  # ok / concerning / alarming
    rationale: str
    underlying: ExternalSupervisorObservation | None  # None 表示 LLM 未调
    evidence_summary: dict[str, Any] = field(default_factory=dict)


# ---- 工程化检查规则 ----


def _detect_self_aggrandizement_signals(
    executor_self_report: dict[str, Any],
    evidence_artifacts: list[dict[str, Any]],
) -> list[str]:
    """工程化规则检测 — 命中即 signal. 多 signal → is_self_aggrandizing.

    规则:
      1. claims_all_done_but_no_test_report:
         self_report.criteria_done == total 但 artifacts 没 test_report
      2. evidence_count_zero:
         artifacts 空 但 self_report 自称完成
      3. fallback_triggered_but_self_report_clean:
         self_report.fallback_triggered == False 但 artifacts 有 fallback 记录
      4. rationale_too_short_vs_complexity:
         self_report.rationale 短 (< 50 字) 但 estimated_steps > 5
      5. mentions_unverified_paths:
         self_report.cited_paths 中有 artifacts 找不到的路径
    """
    signals: list[str] = []

    artifact_kinds = {a.get("kind", "").lower() for a in evidence_artifacts}
    artifact_paths = {
        a.get("path") for a in evidence_artifacts if a.get("path")
    }

    criteria_done = executor_self_report.get("criteria_done_count")
    criteria_total = executor_self_report.get("criteria_total_count")
    claims_done = (
        executor_self_report.get("claims_complete")
        or executor_self_report.get("on_anchor") is True
        or (
            criteria_done is not None
            and criteria_total is not None
            and criteria_done >= criteria_total
        )
    )

    if claims_done and "test_report" not in artifact_kinds:
        signals.append("claims_all_done_but_no_test_report")

    if claims_done and not evidence_artifacts:
        signals.append("evidence_count_zero")

    self_says_no_fallback = executor_self_report.get("fallback_triggered") is False
    artifacts_show_fallback = any(
        a.get("kind") == "llm_fallback_record" for a in evidence_artifacts
    )
    if self_says_no_fallback and artifacts_show_fallback:
        signals.append("fallback_triggered_but_self_report_clean")

    rationale = str(executor_self_report.get("rationale", "") or "")
    estimated_steps = executor_self_report.get("estimated_steps", 0) or 0
    if estimated_steps > 5 and len(rationale) < 50:
        signals.append("rationale_too_short_vs_complexity")

    cited_paths = executor_self_report.get("cited_paths", []) or []
    if isinstance(cited_paths, list):
        for p in cited_paths:
            if p and p not in artifact_paths:
                signals.append(f"unverified_path:{p}")

    return signals


# ---- Mode wrappers ----


async def mode_a_gate_review(
    service: ExternalSupervisorService,
    *,
    anchor: dict[str, Any] | None,
    gate_evidence: dict[str, Any],
    target_task_id: str | None = None,
    target_anchor_id: str | None = None,
) -> GateAdvisory:
    """Mode A · Gate sync 复核 (Director 在 Gate 决策前调)."""
    observation = await service.analyze_observation(
        obs_kind="gate_review",
        observation_payload=gate_evidence,
        anchor=anchor,
        target_task_id=target_task_id,
        target_anchor_id=target_anchor_id,
    )

    # verdict → Gate 决策 mapping
    if observation.verdict == "ok":
        gate_verdict = "approve"
        action = "continue"
    elif observation.verdict == "concerning":
        gate_verdict = "escalate"
        action = "pause"
    else:  # alarming
        gate_verdict = "block"
        action = "human_review"

    return GateAdvisory(
        advisory_id=observation.observation_id,
        verdict=gate_verdict,
        rationale=observation.rationale,
        recommended_action=observation.recommended_action or action,
        underlying=observation,
    )


def _compute_evidence_quality_score(
    artifacts: list[dict[str, Any]],
    self_report: dict[str, Any],
) -> float:
    """工程化打分 [0, 1]: artifact 多 + kind 多样 + 含 test_report → 高."""
    if not artifacts:
        return 0.0
    kinds = {a.get("kind", "") for a in artifacts}
    score = 0.0
    if "test_report" in kinds:
        score += 0.4
    if "artifact_link" in kinds or any(a.get("path") for a in artifacts):
        score += 0.2
    if "decision" in kinds:
        score += 0.1
    # kind 多样性
    score += min(0.2, len(kinds) * 0.05)
    # artifact 总量
    score += min(0.1, len(artifacts) * 0.02)
    return min(1.0, score)


async def mode_b_task_debrief(
    service: ExternalSupervisorService,
    *,
    anchor: dict[str, Any] | None,
    task_summary: dict[str, Any],
    artifacts: list[dict[str, Any]],
    target_task_id: str | None = None,
    target_anchor_id: str | None = None,
) -> DebriefRecord:
    """Mode B · 任务尾复盘 (task done 后异步触发)."""
    quality_score = _compute_evidence_quality_score(artifacts, task_summary)
    observation = await service.analyze_observation(
        obs_kind="task_debrief",
        observation_payload={
            "task_summary": task_summary,
            "artifacts": artifacts,
            "evidence_quality_score": quality_score,
        },
        anchor=anchor,
        target_task_id=target_task_id,
        target_anchor_id=target_anchor_id,
    )

    # 把 quality_score 推到 capability_writeback 建议给 L2.8 Gate 用
    writeback: dict[str, Any] = {
        "evidence_quality_score": quality_score,
        "supervisor_verdict": observation.verdict,
    }
    if observation.recommended_action:
        writeback["recommended_action"] = observation.recommended_action
    if observation.verdict == "alarming":
        writeback["promotion_block_reason"] = observation.rationale

    return DebriefRecord(
        debrief_id=observation.observation_id,
        verdict=observation.verdict,
        rationale=observation.rationale,
        evidence_quality_score=quality_score,
        recommended_capability_writeback=writeback,
        underlying=observation,
        triggered_at=observation.observed_at,
    )


async def check_self_aggrandizement(
    service: ExternalSupervisorService,
    *,
    anchor: dict[str, Any] | None,
    executor_self_report: dict[str, Any],
    evidence_artifacts: list[dict[str, Any]],
    target_task_id: str | None = None,
    target_anchor_id: str | None = None,
    always_call_llm: bool = False,
) -> SelfAggrandizementCheck:
    """自嗨检测 — 每次任务必跑.

    流程:
      1. 工程化规则检测 → engineering_signals
      2. 信号 ≥ 1 或 always_call_llm=True → 调 LLM 二次复核
      3. 综合判定 is_self_aggrandizing
    """
    signals = _detect_self_aggrandizement_signals(executor_self_report, evidence_artifacts)
    should_call_llm = bool(signals) or always_call_llm

    if not should_call_llm:
        # 工程化规则零信号 → 不烧 LLM token
        return SelfAggrandizementCheck(
            check_id=f"sac_{target_task_id or 'unknown'}",
            is_self_aggrandizing=False,
            engineering_signals=[],
            llm_verdict="ok",
            rationale="no engineering signals; LLM call skipped",
            underlying=None,
            evidence_summary={
                "artifact_count": len(evidence_artifacts),
                "always_call_llm": always_call_llm,
            },
        )

    observation = await service.analyze_observation(
        obs_kind="self_aggrandizement_check",
        observation_payload={
            "executor_self_report": executor_self_report,
            "evidence_artifacts": evidence_artifacts,
            "engineering_signals": signals,
        },
        anchor=anchor,
        target_task_id=target_task_id,
        target_anchor_id=target_anchor_id,
    )

    # 工程化 ≥ 2 signal 或 LLM alarming → 判定自嗨
    is_self_aggrandizing = (
        len(signals) >= 2 or observation.verdict == "alarming"
    )

    return SelfAggrandizementCheck(
        check_id=observation.observation_id,
        is_self_aggrandizing=is_self_aggrandizing,
        engineering_signals=signals,
        llm_verdict=observation.verdict,
        rationale=observation.rationale,
        underlying=observation,
        evidence_summary={
            "artifact_count": len(evidence_artifacts),
            "engineering_signal_count": len(signals),
        },
    )


__all__ = [
    "DebriefRecord",
    "GateAdvisory",
    "SelfAggrandizementCheck",
    "check_self_aggrandizement",
    "mode_a_gate_review",
    "mode_b_task_debrief",
]
