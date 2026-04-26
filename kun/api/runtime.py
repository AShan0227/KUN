"""Shared API runtime wiring.

HTTP and WebSocket routes must use the same app-level Orchestrator so the
Watchtower rules loaded during startup are actually used on real requests.

V2.1 wire: 加 safety singletons (FastPath / KillSwitch / TokenMeter) 让
chat / WS 入口共享同一份实例.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from kun.core.emergent_solution import EmergentSolutionLibrary
from kun.engineering.cron_scheduler import CronScheduler
from kun.engineering.emergent_switch import EmergentSwitchManager
from kun.engineering.execution_protocol import StructuredStepGenerator
from kun.engineering.fast_path import FastPathRouter
from kun.engineering.idle_batch import KnowledgePrecipitationStep, register_step
from kun.engineering.marginal_roi import ModulePresets
from kun.engineering.orchestrator import Orchestrator
from kun.engineering.precipitation import (
    KnowledgePrecipitation,
    NarrativeDistillStep,
    RuleEmergeStep,
    StatsWritebackStep,
    WeightTuneStep,
)
from kun.engineering.safety_guards import (
    KillSwitch,
    PlanOnlyGate,
    TaskTimeoutGuard,
    TokenMeter,
    ZeroTelemetryEnforcer,
)
from kun.security.diagnose_runner import DiagnoseRunner
from kun.security.fix_handlers import register_default_fix_handlers
from kun.watchtower.engine import RuleEngine
from kun.watchtower.value_estimators import ProductionValueEstimator
from kun.watchtower.value_gate import ValueGate


class _AppWithState(Protocol):
    state: Any


def install_runtime(app: _AppWithState, *, rule_engine: RuleEngine) -> Orchestrator:
    """Install shared runtime services onto ``app.state``.

    V2.1 安装的 singletons:
    - orchestrator: 主执行
    - rule_engine: 守望规则
    - fast_path: §17.4a 决策跳过快速路径
    - kill_switch: §5.2.3 紧急中断
    - token_meter: §5.2.1 token 仪表盘 + 单步上限
    - plan_only_gate: §5.2.4 destructive 操作 plan-only
    - task_timeout: §5.2.2 任务级超时
    - zero_telemetry: §11.5 零回传
    """
    # V2.1 §5.8: 涌现方案库 + 切换管理器 (信号驱动, 真切给 M5)
    emergent_library = EmergentSolutionLibrary()
    emergent_switch_manager = EmergentSwitchManager(library=emergent_library)
    app.state.emergent_library = emergent_library
    app.state.emergent_switch_manager = emergent_switch_manager

    # V2.1 §16.12: 知识沉淀管道 (4 类内置 step) + 接进 idle_batch
    precipitation = KnowledgePrecipitation()
    precipitation.register_step(StatsWritebackStep())
    precipitation.register_step(WeightTuneStep())
    precipitation.register_step(RuleEmergeStep())
    precipitation.register_step(NarrativeDistillStep())
    app.state.knowledge_precipitation = precipitation
    register_step(KnowledgePrecipitationStep(kp_provider=lambda: app.state.knowledge_precipitation))

    # V2.2 §26 / Wire 26: KUN-Lab → 主仓库闭环
    # KUN_LAB_BRIDGE_ENABLED=1 启用 (默认 off — 跟 KUN_LAB_MODE 解耦, 防意外消费).
    # 启用后:
    #   1. precipitation 收 experiment.promoted 事件 → LabRecipePrecipitationStep
    #      产 AssetUpdate
    #   2. apply_hook 写入 LabRecipeRegistry
    #   3. ExecutionMode classifier 自动查 registry, lab 推荐影响 mode 决策
    #   4. idle_batch.LabRecipeAdoptionStep 周期拉 events 流入闭环
    import os as _os

    if _os.getenv("KUN_LAB_BRIDGE_ENABLED", "0") == "1":
        from kun.lab import (
            LabRecipePrecipitationStep,
            get_recipe_registry,
            install_lab_adoption_step,
            make_kp_adopter,
            make_registry_apply_hook,
        )

        lab_registry = get_recipe_registry()
        precipitation.register_asset_apply_hook(make_registry_apply_hook(lab_registry))
        precipitation.register_step(LabRecipePrecipitationStep())
        install_lab_adoption_step(adopter=make_kp_adopter(precipitation))
        app.state.lab_recipe_registry = lab_registry

    # V2.1 §10.6 / M3.2 提前: 傩诊断 runner + 5 类默认 fix handler
    diagnose_runner = DiagnoseRunner()
    register_default_fix_handlers(diagnose_runner)
    app.state.diagnose_runner = diagnose_runner

    # V2.1 M4: 真 cron scheduler (替换固定 interval idle_batch_worker)
    app.state.cron_scheduler = CronScheduler()

    # V2.2 §19.4 + §21: 守望主决策 gate (默认开, FAST 模式自动跳过)
    # KUN_VALUE_GATE_ENABLED=0 强制关闭整个 gate
    value_gate = None
    if _os.getenv("KUN_VALUE_GATE_ENABLED", "1") == "1":
        # V2.2 Wire 2: 用 production estimator 替代 default heuristic
        # capability_card 历史 + 预算剩余 + multi_judge 一致率 加权
        prod_estimator = ProductionValueEstimator()
        value_gate = ValueGate(
            marginal_criterion=ModulePresets.for_idle_batch_step(),
            min_value_threshold=0.20,
            value_estimator=prod_estimator.estimate,
        )
    app.state.value_gate = value_gate

    # V2.2 §22 + Wire 3: hermes 结构化执行 generator (默认开, FAST 模式自动跳过)
    structured_step_generator = None
    if _os.getenv("KUN_HERMES_ENABLED", "1") == "1":
        from kun.interface.llm import get_router

        structured_step_generator = StructuredStepGenerator(get_router())
    app.state.structured_step_generator = structured_step_generator

    orchestrator = Orchestrator(
        rule_engine=rule_engine,
        emergent_switch_manager=emergent_switch_manager,
        value_gate=value_gate,
        structured_step_generator=structured_step_generator,
    )
    app.state.rule_engine = rule_engine
    app.state.orchestrator = orchestrator

    # V2.1 safety singletons
    app.state.fast_path = FastPathRouter(
        # M3.2 暂用空 lookup, M3.3 接真 cache / template / history
        cache_lookup=None,
        template_lookup=None,
        history_lookup=None,
        deterministic_types=("tools.echo",),
        user_trust_lookup=None,  # M3.3 接 capability_card 查 task_count
    )
    app.state.kill_switch = KillSwitch(sla_ms=500)
    app.state.token_meter = TokenMeter()
    app.state.plan_only_gate = PlanOnlyGate()
    app.state.task_timeout = TaskTimeoutGuard()
    app.state.zero_telemetry = ZeroTelemetryEnforcer()  # 默认关回传
    return orchestrator


def get_orchestrator(app: _AppWithState) -> Orchestrator:
    """Return the shared Orchestrator installed by the FastAPI lifespan."""
    orchestrator = getattr(app.state, "orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("API runtime has not been initialized")
    return cast(Orchestrator, orchestrator)


def get_fast_path(app: _AppWithState) -> FastPathRouter:
    return cast(FastPathRouter, app.state.fast_path)


def get_kill_switch(app: _AppWithState) -> KillSwitch:
    return cast(KillSwitch, app.state.kill_switch)


def get_token_meter(app: _AppWithState) -> TokenMeter:
    return cast(TokenMeter, app.state.token_meter)


def get_plan_only_gate(app: _AppWithState) -> PlanOnlyGate:
    return cast(PlanOnlyGate, app.state.plan_only_gate)


def get_task_timeout(app: _AppWithState) -> TaskTimeoutGuard:
    return cast(TaskTimeoutGuard, app.state.task_timeout)


def get_zero_telemetry(app: _AppWithState) -> ZeroTelemetryEnforcer:
    return cast(ZeroTelemetryEnforcer, app.state.zero_telemetry)


def get_emergent_switch_manager(app: _AppWithState) -> EmergentSwitchManager:
    return cast(EmergentSwitchManager, app.state.emergent_switch_manager)


def get_emergent_library(app: _AppWithState) -> EmergentSolutionLibrary:
    return cast(EmergentSolutionLibrary, app.state.emergent_library)


def get_knowledge_precipitation(app: _AppWithState) -> KnowledgePrecipitation:
    return cast(KnowledgePrecipitation, app.state.knowledge_precipitation)


def get_diagnose_runner(app: _AppWithState) -> DiagnoseRunner:
    return cast(DiagnoseRunner, app.state.diagnose_runner)


def get_cron_scheduler(app: _AppWithState) -> CronScheduler:
    return cast(CronScheduler, app.state.cron_scheduler)


def get_value_gate(app: _AppWithState) -> ValueGate | None:
    """V2.2 §19.4: 守望主决策 gate. 默认 None, env KUN_VALUE_GATE_ENABLED=1 启用."""
    return getattr(app.state, "value_gate", None)


def get_structured_step_generator(app: _AppWithState) -> StructuredStepGenerator | None:
    """V2.2 §22: hermes 结构化执行 generator. 默认开, env KUN_HERMES_ENABLED=0 关."""
    return getattr(app.state, "structured_step_generator", None)
