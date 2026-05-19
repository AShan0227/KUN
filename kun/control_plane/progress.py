"""User-facing progress summaries for KUN V6 missions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.collaboration import CollaborationQueueSummary
from kun.control_plane.runtime import ControlPlaneProgressReport
from kun.control_plane.v6 import FailureCategory

MissionProgressTone = Literal["working", "waiting", "blocked", "ready", "done"]
QualityGateStatus = Literal["unknown", "pass", "needs_repair", "invalid", "blocked"]


class UserProgressSummary(BaseModel):
    """Short non-technical status suitable for chat or UI surfaces."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tone: MissionProgressTone
    current_status: str
    blocking_reason: str = ""
    next_step: str
    quality_gate_status: QualityGateStatus = "unknown"
    human_needed: bool = False
    safe_to_continue: bool = True
    open_ticket_ids: list[str] = Field(default_factory=list)
    ready_work_item_ids: list[str] = Field(default_factory=list)
    latest_failure_category: FailureCategory | None = None


def build_user_progress_summary(
    progress: ControlPlaneProgressReport,
    *,
    collaboration: CollaborationQueueSummary | None = None,
) -> UserProgressSummary:
    """Translate runtime state into a concise user-facing summary."""

    open_tickets = _open_ticket_ids(progress=progress, collaboration=collaboration)
    human_needed = progress.status == "waiting_human" or bool(open_tickets)
    quality_gate_status = _quality_gate_status(progress)
    blocking_reason = _blocking_reason(progress=progress, human_needed=human_needed)
    tone = _tone(progress=progress, human_needed=human_needed)
    safe_to_continue = tone in {"working", "ready"} and quality_gate_status not in {
        "invalid",
        "blocked",
    }
    return UserProgressSummary(
        mission_id=progress.mission_id,
        tone=tone,
        current_status=_current_status(progress),
        blocking_reason=blocking_reason,
        next_step=_next_step(progress=progress, human_needed=human_needed),
        quality_gate_status=quality_gate_status,
        human_needed=human_needed,
        safe_to_continue=safe_to_continue,
        open_ticket_ids=open_tickets,
        ready_work_item_ids=progress.next_ready_work_item_ids,
        latest_failure_category=progress.latest_failure_category,
    )


def _open_ticket_ids(
    *,
    progress: ControlPlaneProgressReport,
    collaboration: CollaborationQueueSummary | None,
) -> list[str]:
    if collaboration is None:
        return list(progress.open_collaboration_ticket_ids)
    return [
        *collaboration.open_ticket_ids,
        *collaboration.waiting_ticket_ids,
        *collaboration.escalated_ticket_ids,
    ]


def _tone(*, progress: ControlPlaneProgressReport, human_needed: bool) -> MissionProgressTone:
    if progress.status in {"closed", "partial_closed"}:
        return "done"
    if progress.status in {"delivering", "awaiting_acceptance", "learning_writeback"}:
        return "ready"
    if human_needed or progress.status in {"waiting_human", "waiting_external", "info_gap"}:
        return "waiting"
    if progress.status in {"blocked", "repairing", "rolling_back", "changing_plan", "failed"}:
        return "blocked"
    return "working"


def _current_status(progress: ControlPlaneProgressReport) -> str:
    done = progress.work_item_counts.get("done", 0)
    total = progress.total_work_items
    if progress.status == "queued":
        return f"任务已排队，当前有 {len(progress.next_ready_work_item_ids)} 个可执行工作项。"
    if progress.status == "running":
        return f"任务正在执行，已完成 {done}/{total} 个工作项。"
    if progress.status == "delivering":
        return "交付物已通过当前门禁，正在进入交付。"
    if progress.status == "awaiting_acceptance":
        return "交付物正在等待验收。"
    if progress.status == "learning_writeback":
        return "结果已被接受，正在写回经验。"
    if progress.status in {"closed", "partial_closed"}:
        return "任务已经收口。"
    return f"任务处于 {progress.status} 状态。"


def _quality_gate_status(progress: ControlPlaneProgressReport) -> QualityGateStatus:
    if progress.latest_gate_verdict == "pass":
        return "pass"
    if progress.latest_gate_verdict == "fail":
        if progress.latest_failure_category in {"environment_failure", "tool_failure"}:
            return "invalid"
        return "needs_repair"
    if progress.status in {"blocked", "failed"}:
        return "blocked"
    return "unknown"


def _blocking_reason(*, progress: ControlPlaneProgressReport, human_needed: bool) -> str:
    if human_needed:
        return "需要用户、审批人或外部协作者回复后才能继续。"
    if progress.latest_failure_category == "environment_failure":
        return "运行环境或外部工具异常，当前结果不能算作能力失败。"
    if progress.latest_failure_category == "tool_failure":
        return "工具、wrapper、路由或比较器异常，需要先修系统问题。"
    if progress.latest_failure_category == "model_quality_failure":
        return "结果质量没有过门禁，需要修复能力或改计划后复测。"
    if progress.status in {"blocked", "repairing", "rolling_back", "changing_plan"}:
        return "任务需要先修复、回滚或调整计划。"
    return ""


def _next_step(*, progress: ControlPlaneProgressReport, human_needed: bool) -> str:
    if human_needed:
        return "等待协作票据回复；收到回复后自动恢复对应工作项。"
    if progress.next_ready_work_item_ids:
        return "继续执行下一个已就绪工作项。"
    if progress.status == "delivering":
        return "生成最终交付并进入验收。"
    if progress.status == "awaiting_acceptance":
        return "等待验收结论；如需返工会自动生成修复工作项。"
    if progress.status == "learning_writeback":
        return "写回经验并检查是否可形成能力候选。"
    if progress.status in {"repairing", "blocked"}:
        return "先执行修复或重跑，确认污染和环境问题已清除。"
    return "继续观察任务状态并保持账本可追溯。"
