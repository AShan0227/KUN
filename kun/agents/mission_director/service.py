"""MissionDirectorService — V7 §9.7 任务级监督服务.

V7 §9.7 Mission Director 一级子系统的核心实现.

设计 (跟其他 V7 service 一致, V7 §13.6 frozen_dataclass_agent_io_contract):
- frozen dataclass IO 契约
- emitter callback 模式 (测试 fake / prod 真 DB writer)
- to_row_payload() helper (留给 caller 落库)
- 不直接 import sqlalchemy session

V7 §10.2.4 "监督主体不冲突" 边界:
- Mission Director → 任务级 (方案 / 闭环)
- 启 (Qi) → 方案级反思 (后置 strategy replay)
- 傩 (Nuo) → 执行级 (代码 / 运行时事件)
- External Supervisor → LLM 行为级 (跨模型 critique, **含监督 Mission Director, §10.2.5**)

Mission Director 自身 drift 防护 (V7 §10.2.5 不变量):
- External Supervisor 会 critique Mission Director 的 PlanChangeProposal 输出
- High 等级 PlanChangeProposal 强制 CollaborationTicket 等用户兜底 (V7 §10.3.3)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.mission_director")


class AlignmentVerdict(StrEnum):
    """MissionAlignmentReview verdict (V7 §9.7)."""

    OK = "ok"  # task 在 TaskPlanVersion 范围内, 健康
    DRIFTING = "drifting"  # 信号: log alignment_status='drifting' (V7 §10.4 弱信号)
    OFF_ANCHOR = "off_anchor"  # 偏离明显, 需 PlanChangeProposal
    NEEDS_HUMAN = "needs_human"  # 重大偏离, 必须人审 (高严重等级)


class PlanChangeSeverity(StrEnum):
    """方案变更严重等级 (V7 §10.3.3 决策权三档)."""

    LOW = "low"  # KUN 自动改 + log (子任务顺序/参数微调)
    MEDIUM = "medium"  # KUN 自动改 + 立即推 NUO panel + 用户可一键回滚
    HIGH = "high"  # CollaborationTicket 等人审 (任务边界/目标/不可逆动作)


@dataclass(frozen=True)
class MissionAlignmentReview:
    """V7 §9.7 Mission Director 周期输出 (每 tick / 每 milestone)."""

    review_id: str
    task_id: str
    task_plan_version: str
    reviewed_at: datetime
    verdict: AlignmentVerdict
    alignment_score: float  # 0.0 (完全偏离) - 1.0 (完美对齐)
    findings: list[str]  # 具体观察: info_gap 未补 / 拆解漏 / 证据缺 等
    info_gap_coverage: float  # 0-1, TaskPlan.info_gaps 已补齐比例
    decomposition_coverage: float  # 0-1, work item 覆盖 TaskPlan deliverable 比例
    evidence_coverage: float  # 0-1, evidence ledger 覆盖 TaskPlan 证据计划比例
    plan_change_proposed: bool = False
    plan_change_proposal_id: str | None = None

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        """Convert to ORM dict (caller writes to mission_alignment_reviews table)."""
        return {
            "tenant_id": tenant_id,
            "review_id": self.review_id,
            "task_id": self.task_id,
            "task_plan_version": self.task_plan_version,
            "reviewed_at": self.reviewed_at,
            "verdict": self.verdict.value,
            "alignment_score": self.alignment_score,
            "findings": self.findings,
            "info_gap_coverage": self.info_gap_coverage,
            "decomposition_coverage": self.decomposition_coverage,
            "evidence_coverage": self.evidence_coverage,
            "plan_change_proposed": self.plan_change_proposed,
            "plan_change_proposal_id": self.plan_change_proposal_id,
        }


@dataclass(frozen=True)
class PlanChangeProposal:
    """V7 §10.3.2 方案变更提案 (方案线发现问题后触发)."""

    proposal_id: str
    task_id: str
    triggered_by: str  # "mission_director" / "qi" (启 strategy replay)
    triggered_at: datetime
    change_type: str  # "scope" / "criteria" / "resource" / "risk"
    severity: PlanChangeSeverity
    affected_work_items: list[str]
    affected_deliverables: list[str]
    candidate_changes: list[dict[str, Any]]  # ≥ 1 候选方案
    rollback_condition: str  # 触发回滚的条件
    rationale: str
    user_approval_required: bool = False  # high severity → True (V7 §10.3.3)

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "triggered_by": self.triggered_by,
            "triggered_at": self.triggered_at,
            "change_type": self.change_type,
            "severity": self.severity.value,
            "affected_work_items": self.affected_work_items,
            "affected_deliverables": self.affected_deliverables,
            "candidate_changes": self.candidate_changes,
            "rollback_condition": self.rollback_condition,
            "rationale": self.rationale,
            "user_approval_required": self.user_approval_required,
        }


# Emitter callback type (V7 §13.6 frozen_dataclass_agent_io_contract)
MissionReviewEmitter = Callable[[MissionAlignmentReview], Awaitable[None]]
PlanChangeProposalEmitter = Callable[[PlanChangeProposal], Awaitable[None]]


class MissionDirectorService:
    """V7 §9.7 任务级监督服务.

    主要 method:
    - review_mission(task_id, task_plan, ...): 跑一次 MissionAlignmentReview
    - propose_plan_change(...): 发现方案错漏时生成 PlanChangeProposal

    依赖注入 (跟其他 V7 service 一致):
    - review_emitter: 把 review 写入 DB
    - proposal_emitter: 把 proposal 写入 DB
    """

    def __init__(
        self,
        *,
        review_emitter: MissionReviewEmitter | None = None,
        proposal_emitter: PlanChangeProposalEmitter | None = None,
        alignment_drift_threshold: float = 0.7,  # < 0.7 → drifting
        alignment_off_anchor_threshold: float = 0.4,  # < 0.4 → off_anchor
        alignment_needs_human_threshold: float = 0.2,  # < 0.2 → needs_human
    ) -> None:
        self._review_emitter = review_emitter
        self._proposal_emitter = proposal_emitter
        self._drift_threshold = alignment_drift_threshold
        self._off_anchor_threshold = alignment_off_anchor_threshold
        self._needs_human_threshold = alignment_needs_human_threshold

    async def review_mission(
        self,
        *,
        task_id: str,
        task_plan_version: str,
        info_gap_coverage: float,  # 0-1
        decomposition_coverage: float,  # 0-1
        evidence_coverage: float,  # 0-1
        observed_findings: list[str] | None = None,
    ) -> MissionAlignmentReview:
        """Run one alignment review tick.

        Args:
            task_id: target task
            task_plan_version: current TaskPlanVersion id (V7 §10.1)
            info_gap_coverage: 0-1, fraction of TaskPlan.info_gaps resolved
            decomposition_coverage: 0-1, work items vs TaskPlan deliverables ratio
            evidence_coverage: 0-1, evidence ledger vs TaskPlan evidence plan ratio
            observed_findings: optional list of human/LLM observations

        Returns:
            MissionAlignmentReview record. Caller decides whether to follow-up
            with propose_plan_change.

        Alignment score = weighted average of the 3 coverages:
            score = 0.4 * info_gap + 0.35 * decomposition + 0.25 * evidence
        """
        # Clamp inputs to [0, 1]
        info_gap_coverage = max(0.0, min(1.0, info_gap_coverage))
        decomposition_coverage = max(0.0, min(1.0, decomposition_coverage))
        evidence_coverage = max(0.0, min(1.0, evidence_coverage))

        # Weighted alignment score (V7 §9.7 priorities)
        alignment_score = (
            0.4 * info_gap_coverage
            + 0.35 * decomposition_coverage
            + 0.25 * evidence_coverage
        )

        # Verdict by threshold (V7 §10.4 三级信号 mapping)
        if alignment_score >= self._drift_threshold:
            verdict = AlignmentVerdict.OK
        elif alignment_score >= self._off_anchor_threshold:
            verdict = AlignmentVerdict.DRIFTING
        elif alignment_score >= self._needs_human_threshold:
            verdict = AlignmentVerdict.OFF_ANCHOR
        else:
            verdict = AlignmentVerdict.NEEDS_HUMAN

        findings = list(observed_findings or [])
        # Auto-generated findings from coverage data
        if info_gap_coverage < 0.5:
            findings.append(
                f"info_gap_coverage={info_gap_coverage:.2f} below 0.5 — "
                f"任务方案信息缺口 ≥ 50% 未补"
            )
        if decomposition_coverage < 0.7:
            findings.append(
                f"decomposition_coverage={decomposition_coverage:.2f} below 0.7 — "
                f"work item 未覆盖全部 TaskPlan deliverable"
            )
        if evidence_coverage < 0.5:
            findings.append(
                f"evidence_coverage={evidence_coverage:.2f} below 0.5 — "
                f"evidence ledger 未覆盖一半 TaskPlan 证据计划"
            )

        review = MissionAlignmentReview(
            review_id=new_id("plan_review"),  # 复用 plan_review prefix (mar-)
            task_id=task_id,
            task_plan_version=task_plan_version,
            reviewed_at=datetime.now(UTC),
            verdict=verdict,
            alignment_score=alignment_score,
            findings=findings,
            info_gap_coverage=info_gap_coverage,
            decomposition_coverage=decomposition_coverage,
            evidence_coverage=evidence_coverage,
        )

        log.info(
            "mission_director.review",
            task_id=task_id,
            task_plan_version=task_plan_version,
            verdict=verdict.value,
            alignment_score=round(alignment_score, 3),
            n_findings=len(findings),
        )

        if self._review_emitter is not None:
            try:
                await self._review_emitter(review)
            except Exception as e:
                log.warning(
                    "mission_director.review_emit_failed",
                    task_id=task_id,
                    review_id=review.review_id,
                    error=str(e),
                )

        return review

    async def propose_plan_change(
        self,
        *,
        task_id: str,
        change_type: str,
        severity: PlanChangeSeverity,
        affected_work_items: list[str],
        affected_deliverables: list[str],
        candidate_changes: list[dict[str, Any]],
        rollback_condition: str,
        rationale: str,
        triggered_by: str = "mission_director",
    ) -> PlanChangeProposal:
        """生成 PlanChangeProposal (V7 §10.3.2).

        Severity 决定决策权 (V7 §10.3.3):
        - LOW: KUN 自动 (user_approval_required=False)
        - MEDIUM: KUN 自动 + 立即通知 (user_approval_required=False, 但 caller 应推 NUO panel)
        - HIGH: 等用户 (user_approval_required=True)
        """
        proposal = PlanChangeProposal(
            proposal_id=new_id("plan_review"),  # 复用 prefix, caller 可分前缀如果需要
            task_id=task_id,
            triggered_by=triggered_by,
            triggered_at=datetime.now(UTC),
            change_type=change_type,
            severity=severity,
            affected_work_items=list(affected_work_items),
            affected_deliverables=list(affected_deliverables),
            candidate_changes=list(candidate_changes),
            rollback_condition=rollback_condition,
            rationale=rationale,
            user_approval_required=(severity == PlanChangeSeverity.HIGH),
        )

        log.info(
            "mission_director.plan_change_proposal",
            task_id=task_id,
            proposal_id=proposal.proposal_id,
            severity=severity.value,
            change_type=change_type,
            user_approval_required=proposal.user_approval_required,
        )

        if self._proposal_emitter is not None:
            try:
                await self._proposal_emitter(proposal)
            except Exception as e:
                log.warning(
                    "mission_director.proposal_emit_failed",
                    task_id=task_id,
                    proposal_id=proposal.proposal_id,
                    error=str(e),
                )

        return proposal


__all__ = [
    "AlignmentVerdict",
    "MissionAlignmentReview",
    "MissionDirectorService",
    "MissionReviewEmitter",
    "PlanChangeProposal",
    "PlanChangeProposalEmitter",
    "PlanChangeSeverity",
]
