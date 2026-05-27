"""Supervisor agent — 监督线常驻 (ADR-020).

实现:
  - base.py                    · Supervisor Protocol (角色契约)
  - service.py                 · SupervisorService 工程化阈值检测 (L2.1)
  - plan_review_heartbeat.py   · Plan Review Heartbeat 长任务防漂 (L2.3)
  - escalation.py              · 三级阈值 + 4 级升级路径 (L3.4)
  - pool.py                    · 多实例 Pool 按 audit 维度分流 (L4.2)
  - anomaly_cluster.py         · 异常聚类 → 自创 RSI 请求 (L5.1)
  - self_created_request.py    · cluster + RCDH 综合 → rich request (L5.4)
"""

from kun.agents.supervisor.anomaly_cluster import (
    AnomalyCluster,
    cluster_anomalies,
    cluster_to_search_request,
)
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
from kun.agents.supervisor.plan_review_prompt import render_plan_review_prompt
from kun.agents.supervisor.plan_review_service import (
    ExternalSupervisorVerify,
    PlanReviewOutcome,
    PlanReviewService,
    VerdictName,
)
from kun.agents.supervisor.pool import SupervisorPool, SupervisorPoolConfig
from kun.agents.supervisor.self_created_request import (
    build_self_created_request,
    enrich_with_diagnostic,
)
from kun.agents.supervisor.service import SupervisorAnomalyState, SupervisorService

__all__ = [
    "AnomalyCluster",
    "EscalationDecision",
    "EscalationLevel",
    "ExternalSupervisorVerify",
    "PlanReviewHeartbeat",
    "PlanReviewOutcome",
    "PlanReviewService",
    "ReviewRequestEmitter",
    "ReviewTrigger",
    "Severity",
    "Supervisor",
    "SupervisorAnomalyState",
    "SupervisorPool",
    "SupervisorPoolConfig",
    "SupervisorService",
    "VerdictName",
    "build_self_created_request",
    "cluster_anomalies",
    "cluster_to_search_request",
    "compute_severity",
    "decide_escalation",
    "derive_action",
    "enrich_with_diagnostic",
    "escalation_path_for",
    "evaluate_executor_self_report",
    "render_plan_review_prompt",
]
