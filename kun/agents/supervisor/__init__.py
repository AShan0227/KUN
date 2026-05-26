"""Supervisor agent — 监督线常驻 (ADR-020).

实现:
  - base.py                    · Supervisor Protocol (角色契约)
  - service.py                 · SupervisorService 工程化阈值检测 (L2.1)
  - plan_review_heartbeat.py   · Plan Review Heartbeat 长任务防漂 (L2.3)
"""

from kun.agents.supervisor.base import Supervisor
from kun.agents.supervisor.plan_review_heartbeat import (
    PlanReviewHeartbeat,
    ReviewRequestEmitter,
    ReviewTrigger,
    derive_action,
    evaluate_executor_self_report,
)
from kun.agents.supervisor.service import SupervisorAnomalyState, SupervisorService

__all__ = [
    "PlanReviewHeartbeat",
    "ReviewRequestEmitter",
    "ReviewTrigger",
    "Supervisor",
    "SupervisorAnomalyState",
    "SupervisorService",
    "derive_action",
    "evaluate_executor_self_report",
]
