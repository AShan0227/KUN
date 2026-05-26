"""Runtime observation priorities for KUN V6 dogfood supervision.

Observation items are the shared KUN/Qi/Nuo/external supervision signal.
High and critical observations are also delivery blockers unless the daemon
knows the blocked action is the action required to clear the observation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.runtime import InMemoryControlPlane

_OBSERVATION_EVIDENCE_LIMIT = 32
_HUMAN_PLAYER_ACCEPTANCE_FAILURES = {
    "fresh_real_player_review_pass",
    "human_acceptance_missing",
    "human_or_target_user_acceptance_missing",
    "human_or_target_user_review_missing",
    "human_player_review_missing",
    "human_review_missing",
    "player_acceptance_missing",
    "target_player_acceptance_missing",
    "target_user_acceptance_missing",
}

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
    items.extend(_capability_consumption_observations(control_plane, mission_id))
    items.extend(_failed_work_observations(control_plane, mission_id))
    items.extend(_quality_gate_observations(control_plane, mission_id))
    items.extend(_delivery_observations(control_plane, mission_id))
    items.extend(_acceptance_rework_loop_observations(control_plane, mission_id))
    return RuntimeObservationReport(
        mission_id=mission_id,
        items=items,
        requires_external_supervision=any(
            "external_supervisor" in item.routes and item.severity in {"medium", "high", "critical"}
            for item in items
        ),
    )


def _capability_consumption_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    current_plan_version = mission.current_plan_version if mission is not None else None
    receipt_work_item_ids = {
        artifact.work_item_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id
        and artifact.work_item_id is not None
        and (
            "capability_behavior_receipt" in artifact.supports
            or {"strategy_optimization_plan", "dynamic_best_strategy"} <= set(artifact.supports)
        )
    }
    missing_receipt_ids: list[str] = []
    for work_item in control_plane.work_items.values():
        if work_item.mission_id != mission_id:
            continue
        if current_plan_version is not None and work_item.task_plan_version != current_plan_version:
            continue
        if work_item.status not in {"done", "partial"}:
            continue
        if not work_item.required_capability_refs:
            continue
        if _is_runtime_observation_followup(work_item):
            continue
        if work_item.work_item_id not in receipt_work_item_ids:
            missing_receipt_ids.append(work_item.work_item_id)
    if not missing_receipt_ids:
        return []
    return [
        RuntimeObservationItem(
            code="capability_consumption_unproven",
            severity="high",
            title="生产能力缺少行为消费证明",
            why_watch="能力只登记到 work item 或 artifact 里，不等于 planner/runner 真的按能力改变了执行行为。",
            recommended_action=(
                "阻断交付或晋级；要求对应 runner 产出 capability_behavior_receipt，"
                "说明哪些 executable directive 被执行、影响了哪个阶段。"
            ),
            routes=["qi", "control_plane", "external_supervisor"],
            evidence_refs=_cap_observation_refs(missing_receipt_ids),
        )
    ]


def _is_runtime_observation_followup(work_item: Any) -> bool:
    return (
        work_item.owner in {"qi", "nuo"}
        and work_item.type in {"governance", "repair", "research", "retest"}
    ) or "-observation-" in work_item.work_item_id


def _has_capability_behavior_receipt(
    *,
    control_plane: InMemoryControlPlane,
    mission_id: str,
    work_item_id: str,
) -> bool:
    for artifact in control_plane.artifacts.values():
        if artifact.mission_id != mission_id or artifact.work_item_id != work_item_id:
            continue
        supports = set(artifact.supports)
        if "capability_behavior_receipt" in supports:
            return True
        if {"strategy_optimization_plan", "dynamic_best_strategy"} <= supports:
            return True
    return False


def _cap_observation_refs(refs: list[str]) -> list[str]:
    """Keep observation evidence bounded so long dogfood histories stay runnable."""

    return refs[-_OBSERVATION_EVIDENCE_LIMIT:]


def _failed_work_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    current_plan_version = mission.current_plan_version if mission is not None else None
    failed_work = [
        item
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.status == "failed"
        and (current_plan_version is None or item.task_plan_version == current_plan_version)
        and not _failed_work_is_human_acceptance_wait(control_plane, item)
    ]
    if not failed_work:
        return []
    failed_ids = [item.work_item_id for item in failed_work]
    recovery_items = [
        item
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and (
            item.owner in {"qi", "nuo"}
            or (
                item.owner == "control-plane"
                and (item.idempotency_key or "").startswith("nuo-recovery:")
            )
        )
        and (current_plan_version is None or item.task_plan_version == current_plan_version)
    ]
    recovered_refs = {
        ref for item in recovery_items if item.status == "done" for ref in item.recovery_refs
    }
    pending_recovery_refs = {
        ref
        for item in recovery_items
        if item.status not in {"done", "cancelled", "failed"}
        for ref in item.recovery_refs
    }
    # A completed Qi/Nuo follow-up is only proof that a recovery path ran. If
    # the original work item is still failed, the recovery loop is still
    # incomplete and needs a real retry/retest or a new plan branch.
    attempted = [work_id for work_id in failed_ids if work_id in recovered_refs]
    unrecovered = [work_id for work_id in failed_ids if work_id not in recovered_refs]
    incomplete = [work_id for work_id in unrecovered if work_id in pending_recovery_refs]
    incomplete = [*attempted, *incomplete]
    missing = [work_id for work_id in unrecovered if work_id not in pending_recovery_refs]
    if not unrecovered and not attempted:
        return []
    observations: list[RuntimeObservationItem] = []
    if incomplete:
        observations.append(
            RuntimeObservationItem(
                code="failed_work_recovery_incomplete",
                severity="high",
                title="失败工作项恢复闭环尚未完成",
                why_watch="Qi/Nuo 已经接手失败项，但恢复任务还没有完成或通过复测。",
                recommended_action="阻断交付，直到恢复工作完成并产生干净复测或治理证据。",
                routes=["nuo", "qi", "external_supervisor"],
                evidence_refs=_cap_observation_refs(incomplete),
            )
        )
    if missing:
        observations.append(
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
                evidence_refs=_cap_observation_refs(missing),
            )
        )
    return observations


def _failed_work_is_human_acceptance_wait(
    control_plane: InMemoryControlPlane,
    work_item: Any,
) -> bool:
    if work_item.owner != "mission-director":
        return False
    gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == work_item.mission_id and gate.subject_ref == work_item.work_item_id
    ]
    if not gates:
        return False
    latest = sorted(gates, key=lambda gate: gate.gate_evaluation_id)[-1]
    return _gate_only_requires_human_player_acceptance(latest)


def _quality_gate_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    current_plan_version = mission.current_plan_version if mission is not None else None
    gate_order = _gate_order_keys(control_plane, mission_id)
    latest_gates_by_subject_stage = _latest_gate_by_subject_stage(control_plane, mission_id)
    weak_gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and (not current_plan_version or gate.task_plan_version == current_plan_version)
        and gate.task_type != "self_improvement"
        and latest_gates_by_subject_stage.get(_gate_subject_stage_key(gate))
        == gate.gate_evaluation_id
        and not _gate_subject_is_runtime_observation_followup(control_plane, gate.subject_ref)
        and not _weak_gate_recovered_by_later_subject_pass(control_plane, gate, gate_order)
        and not _gate_recovered_by_later_recovery_pass(control_plane, gate, gate_order)
        and not _gate_only_requires_human_player_acceptance(gate)
        and (
            gate.north_star_verdict != "pass"
            or gate.result_quality < gate.thresholds.get("result_quality", 0.8)
            or gate.hard_gate_failures
        )
    ]
    if not weak_gates:
        return []
    failed_refs = _cap_observation_refs([gate.gate_evaluation_id for gate in weak_gates])
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


def _gate_failure_name(value: str) -> str:
    return value.split(":", 1)[-1].strip().lower()


def _is_human_player_acceptance_failure(value: str) -> bool:
    name = _gate_failure_name(value)
    return name in _HUMAN_PLAYER_ACCEPTANCE_FAILURES or name.endswith(
        (
            "_human_acceptance_missing",
            "_target_user_acceptance_missing",
            "_target_player_acceptance_missing",
            "_player_acceptance_missing",
        )
    )


def _gate_only_requires_human_player_acceptance(gate: Any) -> bool:
    failures = [failure for failure in gate.hard_gate_failures if failure]
    if failures:
        return all(_is_human_player_acceptance_failure(failure) for failure in failures)
    return gate.next_action == "needs_human" and gate.next_state in {
        "awaiting_acceptance",
        "waiting_human",
    }


def _latest_gate_by_subject_stage(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> dict[str, str]:
    """Return the newest gate per subject and stage.

    A later work-item validation pass must not erase a still-actionable delivery
    or external-supervisor failure for the same work item.
    """

    gate_order = _gate_order_keys(control_plane, mission_id)
    latest: dict[str, Any] = {}
    for gate in control_plane.gate_evaluations.values():
        if gate.mission_id != mission_id:
            continue
        key = _gate_subject_stage_key(gate)
        current = latest.get(key)
        if current is None or gate_order.get(
            gate.gate_evaluation_id, (-1, -1, "", gate.gate_evaluation_id)
        ) > gate_order.get(current.gate_evaluation_id, (-1, -1, "", current.gate_evaluation_id)):
            latest[key] = gate
    return {key: gate.gate_evaluation_id for key, gate in latest.items()}


def _gate_order_keys(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> dict[str, tuple[int, int, str, str]]:
    gate_run_order: dict[str, tuple[int, str]] = {}
    for index, run in enumerate(control_plane.runs.values()):
        if run.gate_evaluation_ref:
            ended_at = run.ended_at.isoformat() if run.ended_at else ""
            gate_run_order[run.gate_evaluation_ref] = (index, ended_at)
    gate_ledger_order: dict[str, int] = {}
    for event in control_plane.ledger_events.values():
        if event.mission_id != mission_id or event.event_type != "gate_evaluation":
            continue
        gate_ref = event.payload.get("gate_evaluation_id")
        if isinstance(gate_ref, str):
            gate_ledger_order[gate_ref] = max(
                event.sequence,
                gate_ledger_order.get(gate_ref, -1),
            )

    order: dict[str, tuple[int, int, str, str]] = {}
    for gate in control_plane.gate_evaluations.values():
        if gate.mission_id != mission_id:
            continue
        run_index, run_time = gate_run_order.get(gate.gate_evaluation_id, (-1, ""))
        order[gate.gate_evaluation_id] = (
            gate_ledger_order.get(gate.gate_evaluation_id, -1),
            run_index,
            run_time,
            gate.gate_evaluation_id,
        )
    return order


def _weak_gate_recovered_by_later_subject_pass(
    control_plane: InMemoryControlPlane,
    gate: Any,
    gate_order: dict[str, tuple[int, int, str, str]],
) -> bool:
    if gate.stage not in {"governance", "workitem", "execution", "test", "retest", "repair"}:
        return False
    subject = control_plane.work_items.get(gate.subject_ref)
    if subject is None or subject.status not in {"done", "delivered"}:
        return False
    current_order = gate_order.get(gate.gate_evaluation_id, (-1, -1, "", gate.gate_evaluation_id))
    for gate in control_plane.gate_evaluations.values():
        if gate.mission_id != subject.mission_id or gate.subject_ref != subject.work_item_id:
            continue
        candidate_order = gate_order.get(
            gate.gate_evaluation_id, (-1, -1, "", gate.gate_evaluation_id)
        )
        if candidate_order <= current_order:
            continue
        if _gate_is_clean_pass(gate):
            return True
    return False


def _gate_recovered_by_later_recovery_pass(
    control_plane: InMemoryControlPlane,
    gate: Any,
    gate_order: dict[str, tuple[int, int, str, str]],
) -> bool:
    """Return whether later clean recovery evidence supersedes an old weak gate."""

    current_order = gate_order.get(gate.gate_evaluation_id, (-1, -1, "", gate.gate_evaluation_id))
    failed_refs = {gate.gate_evaluation_id}
    for candidate in control_plane.gate_evaluations.values():
        if candidate.mission_id != gate.mission_id:
            continue
        if candidate.stage == "governance":
            continue
        candidate_order = gate_order.get(
            candidate.gate_evaluation_id,
            (-1, -1, "", candidate.gate_evaluation_id),
        )
        if candidate_order <= current_order:
            continue
        if not _gate_is_clean_pass(candidate):
            continue
        if failed_refs & _gate_recovery_refs(control_plane, candidate):
            return True
    return False


def _gate_recovery_refs(
    control_plane: InMemoryControlPlane,
    gate: Any,
) -> set[str]:
    refs = {gate.subject_ref, *gate.evidence_refs, *gate.artifact_refs, *gate.review_refs}
    for ref in [*gate.evidence_refs, *gate.artifact_refs, *gate.review_refs]:
        artifact = control_plane.artifacts.get(ref)
        if artifact is None:
            continue
        refs.update(artifact.supports)
        if artifact.work_item_id:
            refs.add(artifact.work_item_id)
    return refs


def _gate_is_clean_pass(gate: Any) -> bool:
    return (
        gate.north_star_verdict == "pass"
        and gate.result_quality >= gate.thresholds.get("result_quality", 0.8)
        and not gate.hard_gate_failures
    )


def _gate_subject_stage_key(gate: Any) -> str:
    return f"{gate.subject_ref}\x1f{gate.stage}"


def _gate_subject_is_runtime_observation_followup(
    control_plane: InMemoryControlPlane,
    subject_ref: str,
) -> bool:
    work_item = control_plane.work_items.get(subject_ref)
    if work_item is not None:
        return _is_runtime_observation_followup(work_item)
    lowered = subject_ref.lower()
    return (
        "-observation-" in lowered
        or lowered.startswith("work-qi-")
        or lowered.startswith("work-nuo-")
    )


def _acceptance_rework_loop_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    if mission is None or not mission.current_plan_version:
        return []
    if mission.acceptance_ref:
        review = control_plane.acceptance_reviews.get(mission.acceptance_ref)
        if review is not None and review.decision == "accepted":
            return []
    current_plan_version = mission.current_plan_version
    if "acceptance-rework" not in current_plan_version:
        return []
    current_chain_base = _acceptance_rework_chain_base(current_plan_version)

    def same_active_chain(plan_version: str | None) -> bool:
        return bool(
            plan_version
            and (
                plan_version == current_plan_version
                or (
                    "acceptance-rework" in plan_version
                    and _acceptance_rework_chain_base(plan_version) == current_chain_base
                )
            )
        )

    pressure_gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and same_active_chain(gate.task_plan_version)
        and gate.governance_signal == "open_acceptance_requires_continued_product_pressure"
    ]
    mission_director_blocks = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and same_active_chain(gate.task_plan_version)
        and "gate_pass_not_product_done" in gate.hard_gate_failures
    ]
    delivery_passes = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and same_active_chain(gate.task_plan_version)
        and gate.stage == "delivery"
        and gate.next_action == "ready_to_deliver"
    ]
    current_delivery_item_ids = {
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.task_plan_version == current_plan_version
        and item.status != "cancelled"
        and (
            item.type == "merge"
            or "final-delivery" in item.work_item_id
            or "final_delivery" in item.work_item_id
            or "final delivery" in item.expected_output.lower()
        )
    }
    current_delivery_candidate_passes = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id
        and same_active_chain(gate.task_plan_version)
        and gate.subject_ref in current_delivery_item_ids
        and gate.north_star_verdict == "pass"
        and not gate.hard_gate_failures
    ]
    active_rework_statuses = {"queued", "running", "blocked", "retrying", "partial", "failed"}
    rework_versions = {
        item.task_plan_version
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.status in active_rework_statuses
        and same_active_chain(item.task_plan_version)
        and "acceptance-rework" in item.task_plan_version
    }
    historical_rework_versions = {
        item.task_plan_version
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.status != "cancelled"
        and same_active_chain(item.task_plan_version)
        and "acceptance-rework" in item.task_plan_version
    }
    repeated_pressure = len(pressure_gates) >= 2 or len(rework_versions) >= 2
    repeated_delivery_rejection = len(delivery_passes) >= 2 and len(mission_director_blocks) >= 2
    old_loop_with_current_delivery_candidate = (
        len(current_delivery_candidate_passes) >= 1 and len(historical_rework_versions) >= 2
    )
    if (
        not repeated_pressure
        and not repeated_delivery_rejection
        and not old_loop_with_current_delivery_candidate
    ):
        return []
    refs = [
        gate.gate_evaluation_id
        for gate in sorted(pressure_gates, key=lambda gate: gate.gate_evaluation_id)
    ]
    refs.extend(
        gate.gate_evaluation_id
        for gate in sorted(mission_director_blocks, key=lambda gate: gate.gate_evaluation_id)
    )
    refs.extend(
        gate.gate_evaluation_id
        for gate in sorted(delivery_passes, key=lambda gate: gate.gate_evaluation_id)
    )
    refs.extend(
        gate.gate_evaluation_id
        for gate in sorted(
            current_delivery_candidate_passes, key=lambda gate: gate.gate_evaluation_id
        )
    )
    refs.extend(sorted(historical_rework_versions))
    refs.extend(sorted(rework_versions))
    return [
        RuntimeObservationItem(
            code="mechanical_acceptance_rework_loop",
            severity="high",
            title="验收返工可能在机械循环",
            why_watch="同一任务反复从交付态被压回返工，说明门禁可能只驱动流程重跑，没有证明产品体验有新的有效增量。",
            recommended_action=(
                "Qi 必须重新审查策略并提出更优路径；Nuo 检查是否是门禁/状态污染；"
                "外部监督只确认真实产品变化和目标用户体感，不接受单纯测试重复通过。"
            ),
            routes=["qi", "nuo", "external_supervisor"],
            evidence_refs=_cap_observation_refs(refs),
        )
    ]


def _acceptance_rework_chain_base(plan_version: str) -> str:
    marker = "-acceptance-rework-"
    if marker not in plan_version:
        return plan_version
    return plan_version.split(marker, 1)[0]


def _delivery_observations(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[RuntimeObservationItem]:
    mission = control_plane.missions.get(mission_id)
    if mission is None:
        return []
    work_items = [
        item for item in control_plane.work_items.values() if item.mission_id == mission_id
    ]
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
                    evidence_refs=_cap_observation_refs(stale_items),
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
        if not delivery_manifest_refs and mission.status not in {
            "delivering",
            "awaiting_acceptance",
            "closed",
        }:
            return [
                RuntimeObservationItem(
                    code="delivery_manifest_missing",
                    severity="high",
                    title="工作项已完成但没有交付清单",
                    why_watch="这会把执行结果留在工程日志里，用户仍拿不到可验收交付包。",
                    recommended_action="让 runner finalize mission，生成 delivery manifest 和最终 gate。",
                    routes=["kun", "control_plane", "external_supervisor"],
                    evidence_refs=_cap_observation_refs([item.work_item_id for item in work_items]),
                )
            ]
    if (
        mission.status in {"delivering", "awaiting_acceptance"}
        and not mission.artifact_manifest_refs
    ):
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
        latest_delivery_manifest_ref = (
            delivery_manifest_refs[-1] if delivery_manifest_refs else None
        )
        acceptance_ticket_open = any(
            ticket.mission_id == mission_id
            and ticket.type in {"review", "user_decision", "approval"}
            and ticket.status in {"open", "waiting"}
            and latest_delivery_manifest_ref is not None
            and ticket.context_ref == latest_delivery_manifest_ref
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
                    evidence_refs=_cap_observation_refs(delivery_manifest_refs),
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
