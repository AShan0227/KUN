"""V3 production dogfood scenario catalog.

This module is an operator-facing bridge between delivery status and real
dogfood. It does not claim production readiness; it turns the current capability
boundary into concrete scenarios that can be monitored, rehearsed, and used as
release evidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kun.engineering.delivery_status import DeliveryCapability, get_v3_delivery_status

DogfoodScenarioStatus = Literal["ready", "limited", "blocked"]


class DogfoodScenario(BaseModel):
    """One V3 dogfood scenario operators can run or track."""

    scenario_id: str
    label: str
    status: DogfoodScenarioStatus
    objective: str
    cadence: str
    covers: list[str] = Field(default_factory=list)
    smoke_command: str
    ready_when: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @property
    def can_run_now(self) -> bool:
        return self.status in {"ready", "limited"}


class DogfoodScenarioReport(BaseModel):
    """Compact report for NUO, scripts, and release checks."""

    summary: dict[DogfoodScenarioStatus, int]
    scenarios: list[DogfoodScenario]
    validation_issues: list[str] = Field(default_factory=list)


def get_v3_ops_dogfood_scenarios(
    items: list[DeliveryCapability] | None = None,
) -> list[DogfoodScenario]:
    """Return the current V3 dogfood scenarios derived from delivery status."""

    by_id = {item.capability_id: item for item in (items or get_v3_delivery_status())}
    return [
        DogfoodScenario(
            scenario_id="mission_long_horizon_resume",
            label="长周期 Mission 自动推进演练",
            status=_scenario_status(by_id, ["long_horizon_tasks", "main_frontend"]),
            objective="创建一个低风险运营 Mission，确认 scheduler/resume/reaper/checkpoint 能持续推进并可见。",
            cadence="daily until V3 launch",
            covers=[
                "Mission resume worker",
                "Mission scheduler",
                "Mission budget/checkpoint summary",
                "main frontend mission cards",
            ],
            smoke_command=(
                "uv run pytest tests/unit/test_mission_worker.py "
                "tests/unit/test_mission_control.py tests/unit/test_mission_api.py"
            ),
            ready_when=[
                "Mission 自动推进不依赖手动按钮",
                "卡死 queued/running 会进入可解释的 failed/blocked 状态",
                "首页能看到预算、checkpoint、下一步状态",
            ],
            blockers=_capability_blockers(by_id, ["long_horizon_tasks", "main_frontend"]),
            evidence=[
                "/api/missions",
                "/api/missions/resume-worker/run-once",
                "/nuo/health/summary",
            ],
        ),
        DogfoodScenario(
            scenario_id="safe_world_action_review",
            label="低风险外部动作审批演练",
            status=_scenario_status(by_id, ["world_gateway", "nuo_manager"]),
            objective="用 local_file.write / email.draft 低风险动作验证审批、预览、执行记录和产物路径。",
            cadence="per release candidate",
            covers=[
                "WorldGateway safe handlers",
                "pending action approval",
                "NUO action panel",
                "audit packet",
            ],
            smoke_command=(
                "uv run pytest tests/unit/test_action_executor.py "
                "tests/unit/test_nuo_action_panel.py"
            ),
            ready_when=[
                "批准前能看到 preview/diff",
                "批准后能看到执行状态和 artifact_ref",
                "不支持的 action_type 会明确 requires_handler=true",
            ],
            blockers=_capability_blockers(by_id, ["world_gateway", "nuo_manager"]),
            evidence=[
                "/nuo/actions/pending",
                "/nuo/health/delivery-status",
            ],
        ),
        DogfoodScenario(
            scenario_id="memory_strategy_reuse_loop",
            label="记忆/策略复用回路演练",
            status=_scenario_status(by_id, ["memory_strategy_reuse"]),
            objective="跑相似任务，确认历史结果、元决策和能力卡会影响下一次策略选择。",
            cadence="weekly",
            covers=[
                "task result memory",
                "process memory",
                "meta decision memory",
                "capability writeback",
            ],
            smoke_command=(
                "uv run pytest tests/unit/test_v3_memory_scoring_gateway.py "
                "tests/unit/test_strategy_router_bridge.py"
            ),
            ready_when=[
                "相似任务能召回上一轮元决策",
                "策略包选择理由包含历史证据",
                "执行后效果写回 capability/memory",
            ],
            blockers=_capability_blockers(by_id, ["memory_strategy_reuse"]),
            evidence=[
                "Context AssetStore",
                "scorecard events",
                "capability writeback",
            ],
        ),
        DogfoodScenario(
            scenario_id="release_ops_smoke",
            label="生产发布前运维 smoke",
            status=_scenario_status(by_id, ["production_deployment"]),
            objective="发布前验证账号、密钥、监控、备份恢复、release checklist 是否达到上线门槛。",
            cadence="per release candidate",
            covers=[
                "readiness probes",
                "secrets hygiene",
                "backup/restore drill",
                "CI/release checklist",
            ],
            smoke_command="uv run python scripts/dogfood_v3_ops_smoke.py --fail-on-blocked",
            ready_when=[
                "正式账号体系和租户 onboarding 已接入",
                "密钥由部署环境注入且不会进入仓库",
                "备份恢复演练有成功证据",
                "线上监控有告警和仪表盘",
            ],
            blockers=_capability_blockers(by_id, ["production_deployment"]),
            evidence=[
                "/health/ready",
                "/metrics",
                "docs/ops/release-checklist.md",
            ],
        ),
    ]


def dogfood_scenario_summary(
    scenarios: list[DogfoodScenario] | None = None,
) -> dict[DogfoodScenarioStatus, int]:
    counts: dict[DogfoodScenarioStatus, int] = {"ready": 0, "limited": 0, "blocked": 0}
    for scenario in scenarios or get_v3_ops_dogfood_scenarios():
        counts[scenario.status] += 1
    return counts


def validate_ops_dogfood_scenarios(
    scenarios: list[DogfoodScenario] | None = None,
) -> list[str]:
    issues: list[str] = []
    for scenario in scenarios or get_v3_ops_dogfood_scenarios():
        if scenario.status == "ready" and scenario.blockers:
            issues.append(f"{scenario.scenario_id}: ready scenario still has blockers")
        if scenario.status == "blocked" and not scenario.blockers:
            issues.append(f"{scenario.scenario_id}: blocked scenario needs explicit blockers")
        if not scenario.smoke_command.strip():
            issues.append(f"{scenario.scenario_id}: missing smoke command")
        if not scenario.ready_when:
            issues.append(f"{scenario.scenario_id}: missing ready_when criteria")
    return issues


def dogfood_scenario_report(
    scenarios: list[DogfoodScenario] | None = None,
) -> DogfoodScenarioReport:
    items = scenarios or get_v3_ops_dogfood_scenarios()
    return DogfoodScenarioReport(
        summary=dogfood_scenario_summary(items),
        scenarios=items,
        validation_issues=validate_ops_dogfood_scenarios(items),
    )


def _scenario_status(
    by_id: dict[str, DeliveryCapability],
    required_capabilities: list[str],
) -> DogfoodScenarioStatus:
    required = [by_id.get(capability_id) for capability_id in required_capabilities]
    if any(item is None for item in required):
        return "blocked"
    present = [item for item in required if item is not None]
    if any(item.status == "not_ready" for item in present):
        return "blocked"
    if any(item.status in {"partial", "audit_only"} or item.missing for item in present):
        return "limited"
    return "ready"


def _capability_blockers(
    by_id: dict[str, DeliveryCapability],
    required_capabilities: list[str],
) -> list[str]:
    blockers: list[str] = []
    for capability_id in required_capabilities:
        item = by_id.get(capability_id)
        if item is None:
            blockers.append(f"{capability_id}: capability status missing")
            continue
        blockers.extend(f"{item.capability_id}: {missing}" for missing in item.missing)
    return blockers


__all__ = [
    "DogfoodScenario",
    "DogfoodScenarioReport",
    "DogfoodScenarioStatus",
    "dogfood_scenario_report",
    "dogfood_scenario_summary",
    "get_v3_ops_dogfood_scenarios",
    "validate_ops_dogfood_scenarios",
]
