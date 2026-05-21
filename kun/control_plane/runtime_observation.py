"""Runtime observation priorities for KUN V6 dogfood supervision.

Observation items are not blockers by themselves.  They are the shared watchlist
that KUN, Qi, Nuo, and an external supervisor use to decide what to inspect
while real long tasks run.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.runtime import InMemoryControlPlane

ObservationSeverity = Literal["info", "low", "medium", "high", "critical"]
ObservationRoute = Literal["kun", "qi", "nuo", "human", "external_supervisor", "control_plane"]


class RuntimeObservationItem(BaseModel):
    """One thing the system should explicitly watch during real execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: ObservationSeverity
    title: str
    why_watch: str
    recommended_action: str
    routes: list[ObservationRoute] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class RuntimeObservationReport(BaseModel):
    """Mission-level observation report emitted by the daemon."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "kun-v6-runtime-observation-report-v1"
    mission_id: str
    requires_external_supervision: bool = False
    items: list[RuntimeObservationItem] = Field(default_factory=list)

    @property
    def max_severity(self) -> ObservationSeverity:
        if not self.items:
            return "info"
        order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        return max((item.severity for item in self.items), key=lambda severity: order[severity])


def build_runtime_observation_report(
    *,
    control_plane: InMemoryControlPlane,
    mission_id: str,
    tick_report: Any,
    capability_policy: CapabilityExecutionPolicy,
) -> RuntimeObservationReport:
    """Build the shared KUN/Qi/Nuo/external supervision watchlist."""

    items: list[RuntimeObservationItem] = []
    no_runner_ids = list(getattr(tick_report, "no_runner_work_item_ids", []) or [])
    if no_runner_ids:
        items.append(
            RuntimeObservationItem(
                code="runner_missing",
                severity="critical",
                title="有工作项没有执行器",
                why_watch="Control Plane 能看到任务，但没有 runner 会真正执行它。",
                recommended_action="先注册通用 runner 或任务专用 runner；不要让任务空转。",
                routes=["control_plane", "qi", "external_supervisor"],
                evidence_refs=no_runner_ids,
            )
        )
    failed_skills = list(getattr(tick_report, "preflight_failed_skill_ids", []) or [])
    if failed_skills:
        items.append(
            RuntimeObservationItem(
                code="preflight_skill_failed",
                severity="high",
                title="预执行 skill 失败",
                why_watch="这通常是工具、权限、沙箱、网络或环境阻断，不能直接算 KUN 能力失败。",
                recommended_action="交给 Nuo 先分类修复，再由 Qi 判断是否需要能力治理。",
                routes=["nuo", "qi", "external_supervisor"],
                evidence_refs=list(getattr(tick_report, "preflight_artifact_refs", []) or []),
            )
        )
    ticket_ids = list(getattr(tick_report, "created_collaboration_ticket_ids", []) or [])
    if ticket_ids:
        items.append(
            RuntimeObservationItem(
                code="human_ticket_opened",
                severity="medium",
                title="任务请求人类或专家输入",
                why_watch="人机协同是必要能力，但过早、过多或问错人会拖慢长任务。",
                recommended_action="外部监督检查问题是否必要、清楚、可回答，并确认回复后能自动恢复。",
                routes=["human", "external_supervisor", "qi"],
                evidence_refs=ticket_ids,
            )
        )
    fired_rules = list(getattr(tick_report, "watchtower_fired_rule_ids", []) or [])
    if fired_rules:
        items.append(
            RuntimeObservationItem(
                code="watchtower_rule_fired",
                severity="medium",
                title="守望规则触发",
                why_watch="规则触发说明系统发现异常、风险或治理信号，需要判断是否误报或应进入修复。",
                recommended_action="Nuo 判断风险真实性；Qi 判断规则是否该保留、合并或降噪。",
                routes=["nuo", "qi", "external_supervisor"],
                evidence_refs=fired_rules,
            )
        )
    if int(getattr(tick_report, "watchtower_error_count", 0) or 0) > 0:
        items.append(
            RuntimeObservationItem(
                code="watchtower_error",
                severity="high",
                title="守望规则执行报错",
                why_watch="监督机制自身报错会让异常检测失效。",
                recommended_action="先修 Watchtower 规则/输入命名空间，再继续依赖自动监督。",
                routes=["control_plane", "nuo", "external_supervisor"],
                evidence_refs=[],
            )
        )
    governance = control_plane.govern_default_runtime_capabilities()
    if governance.duplicate_profile_refs:
        items.append(
            RuntimeObservationItem(
                code="capability_duplicates_collapsed",
                severity="medium",
                title="生产能力存在重复并被治理折叠",
                why_watch="重复 production capability 会增加规划、runner 和监督噪音。",
                recommended_action="Qi 应确认被折叠能力是否合并、淘汰或降级为证据。",
                routes=["qi", "external_supervisor"],
                evidence_refs=list(governance.duplicate_profile_refs),
            )
        )
    if capability_policy.capability_profile_refs and not capability_policy.directives:
        items.append(
            RuntimeObservationItem(
                code="capability_policy_without_directives",
                severity="high",
                title="生产能力没有转成执行指令",
                why_watch="能力只在档案里存在，没有进入执行路径，就是未激活。",
                recommended_action="Qi 必须把能力转成 planner/runner/supervisor/diagnostics 等指令或降级。",
                routes=["qi", "external_supervisor"],
                evidence_refs=list(capability_policy.capability_profile_refs),
            )
        )
    items.extend(_failed_work_observations(control_plane, mission_id))
    items.extend(_quality_gate_observations(control_plane, mission_id))
    items.extend(_delivery_observations(control_plane, mission_id))
    return RuntimeObservationReport(
        mission_id=mission_id,
        items=items,
        requires_external_supervision=any(
            "external_supervisor" in item.routes and item.severity in {"medium", "high", "critical"}
            for item in items
        ),
    )


def _failed_work_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    failed_work = [
        item
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id and item.status == "failed"
    ]
    if not failed_work:
        return []
    failed_ids = [item.work_item_id for item in failed_work]
    recovered_refs = {
        ref
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id and item.owner in {"qi", "nuo"}
        for ref in item.recovery_refs
    }
    unrecovered = [work_id for work_id in failed_ids if work_id not in recovered_refs]
    if not unrecovered:
        return []
    return [
        RuntimeObservationItem(
            code="failed_work_without_recovery",
            severity="high",
            title="失败工作项缺少恢复闭环",
            why_watch="工作项失败后必须由 Nuo 归因、Qi 调整策略或能力，而不是留给外部监督者发现。",
            recommended_action=(
                "Nuo 判断工具/环境/权限/包装器问题还是真实 KUN 能力失败；Qi 决定继续迭代、"
                "换执行路径、补 runner，或把失败沉淀成能力治理。"
            ),
            routes=["nuo", "qi", "external_supervisor"],
            evidence_refs=unrecovered,
        )
    ]


def _quality_gate_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    weak_gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and (
            gate.north_star_verdict != "pass"
            or gate.result_quality < gate.thresholds.get("result_quality", 0.8)
            or gate.hard_gate_failures
        )
    ]
    if not weak_gates:
        return []
    failed_refs = [gate.gate_evaluation_id for gate in weak_gates]
    responsibility_scopes = {gate.responsibility_scope for gate in weak_gates}
    routes: list[ObservationRoute] = ["qi", "external_supervisor"]
    if responsibility_scopes & {"environment", "mixed", "unknown"}:
        routes.insert(0, "nuo")
    return [
        RuntimeObservationItem(
            code="quality_gate_not_passed",
            severity="high",
            title="结果质量门禁未通过",
            why_watch="任务执行不好时，KUN 必须主动审核、打分、归因并重排策略，不能等待外部监督者指出。",
            recommended_action=(
                "Nuo 先区分系统/环境/污染与真实能力失败；Qi 再打开更优路径、调整计划、"
                "补强验收标准并安排复测。"
            ),
            routes=routes,
            evidence_refs=failed_refs,
        )
    ]


def _delivery_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    if mission is None:
        return []
    work_items = [item for item in control_plane.work_items.values() if item.mission_id == mission_id]
    if mission.current_plan_version:
        stale_items = [
            item.work_item_id
            for item in work_items
            if item.task_plan_version != mission.current_plan_version
            and item.status not in {"done", "cancelled"}
        ]
        if stale_items:
            return [
                RuntimeObservationItem(
                    code="superseded_work_not_retired",
                    severity="medium",
                    title="旧计划工作项仍未归档",
                    why_watch="后续计划已经覆盖旧阻断，但旧 queued/blocked/failed 项继续留在活跃状态会污染驾驶舱和监督判断。",
                    recommended_action="Control Plane 应自动取消被当前计划覆盖的旧工作项，并留下 superseded cleanup 证据。",
                    routes=["control_plane", "qi", "external_supervisor"],
                    evidence_refs=stale_items,
                )
            ]
    if work_items and all(item.status in {"done", "partial"} for item in work_items):
        delivery_manifest_refs = [
            ref
            for ref in mission.artifact_manifest_refs
            if (manifest := control_plane.artifact_manifests.get(ref)) is not None
            and manifest.kind == "delivery"
            and manifest.supports_delivery
        ]
        if not delivery_manifest_refs and mission.status not in {"delivering", "awaiting_acceptance", "closed"}:
            return [
                RuntimeObservationItem(
                    code="delivery_manifest_missing",
                    severity="high",
                    title="工作项已完成但没有交付清单",
                    why_watch="这会把执行结果留在工程日志里，用户仍拿不到可验收交付包。",
                    recommended_action="让 runner finalize mission，生成 delivery manifest 和最终 gate。",
                    routes=["kun", "control_plane", "external_supervisor"],
                    evidence_refs=[item.work_item_id for item in work_items],
                )
            ]
    if mission.status in {"delivering", "awaiting_acceptance"} and not mission.artifact_manifest_refs:
        return [
            RuntimeObservationItem(
                code="delivering_without_manifest",
                severity="critical",
                title="任务进入交付态但没有 artifact manifest",
                why_watch="这说明状态和交付物脱节，用户无法验收。",
                recommended_action="暂停交付态，补齐 manifest、证据和 gate 后再交付。",
                routes=["control_plane", "nuo", "external_supervisor"],
                evidence_refs=[mission.mission_id],
            )
        ]
    if mission.status in {"delivering", "awaiting_acceptance"} and mission.acceptance_ref is None:
        delivery_manifest_refs = [
            ref
            for ref in mission.artifact_manifest_refs
            if (manifest := control_plane.artifact_manifests.get(ref)) is not None
            and manifest.kind == "delivery"
            and manifest.supports_delivery
        ]
        acceptance_ticket_open = any(
            ticket.mission_id == mission_id
            and ticket.type in {"review", "user_decision", "approval"}
            and ticket.status in {"open", "waiting"}
            and (ticket.context_ref in delivery_manifest_refs or "acceptance" in ticket.ticket_id)
            for ticket in control_plane.collaboration_tickets.values()
        )
        if delivery_manifest_refs and not acceptance_ticket_open:
            return [
                RuntimeObservationItem(
                    code="human_acceptance_ticket_missing",
                    severity="high",
                    title="交付态缺少人工验收票据",
                    why_watch="产品体验好不好不能只靠内部门禁；进入交付态后必须主动请求人类或目标用户验收。",
                    recommended_action="自动打开 acceptance/review ticket，注明交付物、验收问题、超时和恢复策略。",
                    routes=["human", "control_plane", "external_supervisor"],
                    evidence_refs=delivery_manifest_refs,
                )
            ]
    return []


__all__ = [
    "ObservationRoute",
    "ObservationSeverity",
    "RuntimeObservationItem",
    "RuntimeObservationReport",
    "build_runtime_observation_report",
]
