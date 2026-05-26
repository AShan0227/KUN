"""Supervisor agent — 监督线常驻 (ADR-020).

实现:
  - base.py                    · Supervisor Protocol (角色契约)
  - service.py                 · SupervisorService 工程化阈值检测 (L2.1)
  - plan_review_heartbeat.py   · Plan Review Heartbeat 长任务防漂 (L2.3)
  - escalation.py              · 三级阈值 + 4 级升级路径 (L3.4)
  - pool.py                    · 多实例 Pool 按 audit 维度分流 (L4.2)
"""

from kun.agents.supervisor.base import Supervisor
from kun.agents.supervisor.escalation import (
    EscalationDecision,
    EscalationLevel,
    Severity,
    compute_severity,
    decide_escalation,
    escalation_path_for,
)
from kun.agents.supervisor.plan_review_heartbeat import (
    PlanReviewHeartbeat,
    ReviewRequestEmitter,
    ReviewTrigger,
    derive_action,
    evaluate_executor_self_report,
)
from kun.agents.supervisor.pool import SupervisorPool, SupervisorPoolConfig
from kun.agents.supervisor.service import SupervisorAnomalyState, SupervisorService

__all__ = [
    "EscalationDecision",
    "EscalationLevel",
    "PlanReviewHeartbeat",
    "ReviewRequestEmitter",
    "ReviewTrigger",
    "Severity",
    "Supervisor",
    "SupervisorAnomalyState",
    "SupervisorPool",
    "SupervisorPoolConfig",
    "SupervisorService",
    "compute_severity",
    "decide_escalation",
    "derive_action",
    "escalation_path_for",
    "evaluate_executor_self_report",
]
