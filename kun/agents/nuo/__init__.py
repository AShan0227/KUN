"""傩 (Nuo) — 质量治理与恢复系统 (V7 §9.5).

V7 命名约定 (§5.2): 傩 (Nuo) 是 KUN 6.5 质量治理与恢复系统的负责人, 主理
污染识别 / 修复 / 复测 / RCDH 4 层诊断 / Watchtower 规则触发响应。

V7 命名迁移 Phase A: 本 module 是 ``kun.agents.supervisor`` 的 re-export 包,
新代码统一 import ``kun.agents.nuo``, 旧代码继续可用 ``kun.agents.supervisor``
(渐进迁移, supervisor 这个英文名是 V6 历史包袱, V7 改 nuo 跟"质量治理与
恢复"语义更贴)。

未来定位 (V7 §5.4): 傩可能独立作为 agent 产品发布, 本 module path 是预留的
对外接口边界。

公共 API (re-export from ``kun.agents.supervisor``):

    from kun.agents.nuo import SupervisorService, PlanReviewService, ...

或新代码用别名:

    from kun.agents.nuo import Nuo  # alias for SupervisorService
"""

from __future__ import annotations

from kun.agents.supervisor import (
    AnomalyCluster,
    EscalationDecision,
    EscalationLevel,
    ExternalSupervisorVerify,
    PlanReviewHeartbeat,
    PlanReviewOutcome,
    PlanReviewService,
    ReviewRequestEmitter,
    ReviewTrigger,
    Severity,
    Supervisor,
    SupervisorAnomalyState,
    SupervisorPool,
    SupervisorPoolConfig,
    SupervisorService,
    VerdictName,
    build_self_created_request,
    cluster_anomalies,
    cluster_to_search_request,
    compute_severity,
    decide_escalation,
    derive_action,
    enrich_with_diagnostic,
    escalation_path_for,
    evaluate_executor_self_report,
    render_plan_review_prompt,
)

# Public alias — 傩 (Nuo) 作为对外角色名, 内部实现 SupervisorService.
# 后续傩独立发布时, ``class Nuo`` 是 public API class name.
Nuo = SupervisorService

__all__ = [
    "AnomalyCluster",
    "EscalationDecision",
    "EscalationLevel",
    "ExternalSupervisorVerify",
    "Nuo",
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
