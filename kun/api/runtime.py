"""Shared API runtime wiring.

HTTP and WebSocket routes must use the same app-level Orchestrator so the
Watchtower rules loaded during startup are actually used on real requests.

V2.1 wire: 加 safety singletons (FastPath / KillSwitch / TokenMeter) 让
chat / WS 入口共享同一份实例.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from kun.core.emergent_solution import EmergentSolutionLibrary
from kun.core.state_ledger import get_state_ledger
from kun.engineering.capability_cache import CapabilityCardCache
from kun.engineering.cron_scheduler import CronScheduler
from kun.engineering.emergent_switch import EmergentSwitchManager
from kun.engineering.execution_protocol import StructuredStepGenerator
from kun.engineering.fast_path import FastPathRouter
from kun.engineering.idle_batch import (
    IncidentLessonDistillStep,
    KnowledgePrecipitationStep,
    register_step,
)
from kun.engineering.marginal_roi import ModulePresets
from kun.engineering.mission_worker import MissionOrchestratorRunner, MissionResumeWorker
from kun.engineering.orchestrator import Orchestrator
from kun.engineering.pending_task_resume import PendingTaskResumeWorker
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
from kun.interface.hermes import DefaultHermesAdapter, NoopHermesAdapter
from kun.memory.writeback import MemoryWriteback
from kun.security.diagnose_runner import DiagnoseRunner
from kun.security.fix_handlers import register_default_fix_handlers
from kun.security.incident_response import IncidentResponseEngine
from kun.watchtower.decision_plane import WatchtowerDecisionPlane
from kun.watchtower.engine import RuleEngine
from kun.watchtower.scoring import UnifiedScoringSystem
from kun.watchtower.value_estimators import ProductionValueEstimator
from kun.watchtower.value_gate import ValueGate
from kun.world.gateway import WorldGateway, set_world_gateway


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
    # V2.2 C44: watchtower fired rules → IncidentResponse → idle-batch lessons.
    incident_response = IncidentResponseEngine()
    rule_engine.set_incident_response(incident_response)
    app.state.incident_response = incident_response
    register_step(IncidentLessonDistillStep(incident_provider=lambda: app.state.incident_response))

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
        from kun.lab.recipe_registry import SqlLabRecipeStorage

        lab_registry = get_recipe_registry(storage=SqlLabRecipeStorage())
        precipitation.register_asset_apply_hook(make_registry_apply_hook(lab_registry))
        precipitation.register_step(LabRecipePrecipitationStep())
        install_lab_adoption_step(adopter=make_kp_adopter(precipitation))
        app.state.lab_recipe_registry = lab_registry

    # Batch9 C29: optional DB-backed ExperimentLog. The singleton factory still
    # defaults to in-memory, but when env opts in we expose the installed log on
    # app.state for CLI/API parity.
    if _os.getenv("KUN_LAB_DB_BACKED", "0") == "1":
        from kun.lab import get_experiment_log

        app.state.lab_experiment_log = get_experiment_log()

    # V2.3: 启 runtime 默认 ON (内测阶段, KUN 还没真用户, 不需 backward-compat).
    # KUN_QI_RUNTIME_ENABLED=0 强制关闭 (e.g. CI 或 minimal 测试).
    if _os.getenv("KUN_QI_RUNTIME_ENABLED", "1") == "1":
        # V2.3: SQL backend default ON (内测), 协议跨重启不丢.
        # KUN_QI_PROTOCOL_DB_ENABLED=0 强制 InMemory (测试 / minimal 部署).
        import logging as _logging

        from kun.engineering.capability_cache import (
            CapabilityCardCache,
            set_capability_card_cache,
        )
        from kun.qi import (
            PheromoneStorage,
            ProtocolRegistry,
            SqlProtocolStorage,
            get_pheromone_storage,
            get_protocol_registry,
            get_qi_budget,
            set_pheromone_storage,
            set_protocol_registry,
        )
        from kun.qi.problem_queue import get_configured_qi_problem_queue

        _local_log = _logging.getLogger("kun.api.runtime")
        if _os.getenv("KUN_QI_PROTOCOL_DB_ENABLED", "1") == "1":
            try:
                protocol_registry = ProtocolRegistry(SqlProtocolStorage())
                set_protocol_registry(protocol_registry)
            except Exception:
                # SQL 装失败 (e.g. DB 没启) → fallback InMemory
                _local_log.exception("protocol.sql_storage_init_failed_fallback_inmemory")
                protocol_registry = get_protocol_registry()
        else:
            protocol_registry = get_protocol_registry()
        app.state.protocol_registry = protocol_registry

        # V2.3: Pheromone SQL backend default ON (内测).
        if _os.getenv("KUN_QI_PHEROMONE_DB_ENABLED", "1") == "1":
            try:
                pheromone_storage = PheromoneStorage()
                set_pheromone_storage(pheromone_storage)
            except Exception:
                _local_log.exception("pheromone.sql_storage_init_failed_fallback_inmemory")
                pheromone_storage = cast(Any, get_pheromone_storage())
        else:
            pheromone_storage = cast(Any, get_pheromone_storage())
        app.state.pheromone_storage = pheromone_storage

        qi_budget = get_qi_budget()
        qi_budget.set_daily_limit(float(_os.getenv("KUN_QI_DAILY_BUDGET_USD", "5.0")))
        app.state.qi_budget = qi_budget
        app.state.qi_problem_queue = get_configured_qi_problem_queue()

        capability_cache = CapabilityCardCache(
            ttl_sec=float(_os.getenv("KUN_CAPABILITY_CACHE_TTL_SEC", "30"))
        )
        set_capability_card_cache(capability_cache)
        app.state.capability_card_cache = capability_cache

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

    # V3: 守望决策层. 这是系统级 MoE 的第一刀:
    # task → StrategyPack → execution_mode/context_limit/skill_hints/metric dimensions.
    # 默认开, 但只产轻量 deterministic 决策, 不额外调用 LLM.
    decision_plane = None
    if _os.getenv("KUN_WATCHTOWER_DECISION_PLANE_ENABLED", "1") == "1":
        decision_plane = WatchtowerDecisionPlane()
    app.state.watchtower_decision_plane = decision_plane

    # V3-2: State Ledger — 任务当前状态的热视图.
    # RuntimeStateRow/EventRow 仍负责持久化; ledger 给 UI/LLM/黑板读当前快照.
    state_ledger = None
    if _os.getenv("KUN_STATE_LEDGER_ENABLED", "1") == "1":
        state_ledger = get_state_ledger()
    app.state.state_ledger = state_ledger

    # V3-4 / V3-6: execution memories and scorecards.  Both are lightweight
    # runtime services; they write through existing context/capability paths.
    memory_writeback = MemoryWriteback()
    scoring_system = UnifiedScoringSystem()
    app.state.memory_writeback = memory_writeback
    app.state.scoring_system = scoring_system

    # V3-3: Hermes full-chain adapter — LLM prompt / skill I/O / external
    # adapter formatting all pass through the same translation layer.
    hermes_adapter: DefaultHermesAdapter | NoopHermesAdapter
    if _os.getenv("KUN_HERMES_ADAPTER_ENABLED", "1") == "1":
        hermes_adapter = DefaultHermesAdapter()
    else:
        hermes_adapter = NoopHermesAdapter()
    app.state.hermes_adapter = hermes_adapter

    # V3-5: World Gateway — central side-effect preparation/audit.
    world_gateway = WorldGateway(hermes_adapter=hermes_adapter)
    set_world_gateway(world_gateway)
    app.state.world_gateway = world_gateway

    # V2.2 §22 + Wire 3: hermes 结构化执行 generator (默认开, FAST 模式自动跳过)
    # Wire 35: 加 ThoughtActionConsistency checker → 自动 rethink (max 2 次)
    structured_step_generator = None
    if _os.getenv("KUN_HERMES_ENABLED", "1") == "1":
        from kun.engineering.execution_protocol import (
            ThoughtActionConsistency,
            make_jury_consistency_judge,
            make_lite_jury_consistency_judge,
        )
        from kun.interface.llm import get_router

        consistency_threshold = float(_os.getenv("KUN_HERMES_CONSISTENCY_THRESHOLD", "0.5"))
        max_rethinks = int(_os.getenv("KUN_HERMES_MAX_RETHINKS", "2"))
        router = get_router()
        jury_enabled = _os.getenv("KUN_HERMES_CONSISTENCY_JURY_ENABLED", "1") == "1"
        jury_judge = None
        lite_jury_judge = None
        if jury_enabled:
            jury_judge = make_jury_consistency_judge(
                router,
                judge_count=int(_os.getenv("KUN_HERMES_CONSISTENCY_JURY_JUDGES", "5")),
            )
            if _os.getenv("KUN_HERMES_SMART_LITE_JURY_ENABLED", "1") == "1":
                lite_jury_judge = make_lite_jury_consistency_judge(
                    router,
                    judge_count=int(_os.getenv("KUN_HERMES_SMART_LITE_JURY_JUDGES", "3")),
                )
        structured_step_generator = StructuredStepGenerator(
            router,
            consistency_checker=ThoughtActionConsistency(
                consistency_threshold=consistency_threshold,
                llm_judge=jury_judge,
                lite_llm_judge=lite_jury_judge,
            ),
            max_rethinks=max_rethinks,
        )
    app.state.structured_step_generator = structured_step_generator

    # V2.2 Wire 36 (BATCH4 C3 / T53): VerificationRunner — task done 前真验证
    # 默认开, env KUN_VERIFICATION_ENABLED=0 关
    verification_runner = None
    if _os.getenv("KUN_VERIFICATION_ENABLED", "1") == "1":
        from kun.engineering.verification_runner import (
            PendingActionApprovalStore,
            VerificationRunner,
        )

        verification_runner = VerificationRunner(
            approval_store=PendingActionApprovalStore(),
        )
    app.state.verification_runner = verification_runner

    # V2.3 Wire 41/42: Predictive Coding hook (插件式)
    # 默认装 in-memory log + InMemoryUpdater. 启训完输出 prediction_model 后,
    # cron 会热安装到 app.state/orchestrator；重启时也可从 KUN_PC_MODEL_PATH load.
    # 没装 → 鲲行为完全不变.
    pc_provider = None
    pc_updater = None
    if _os.getenv("KUN_PREDICTIVE_CODING_ENABLED", "1") == "1":
        from kun.qi.predictive_coding import (
            PredictionLogModelUpdater,
            get_prediction_log,
        )

        pc_log = get_prediction_log()
        pc_updater = PredictionLogModelUpdater(pc_log)
        # pc_provider 默认 None — 启训出 model 后, install_runtime 二次启动会 load
        # 用户可 set KUN_PC_MODEL_PATH 指定 model file
        model_path = _os.getenv("KUN_PC_MODEL_PATH")
        if model_path:
            try:
                from kun.qi.predictive_coding import load_model

                _model = load_model(model_path)
                pc_provider = _model  # PredictionModel 实现 .predict
            except Exception:
                pass  # 没 model 文件就算了, hook 不破
        app.state.predictive_coding_log = pc_log
    app.state.predictive_coding_updater = pc_updater
    app.state.predictive_coding_provider = pc_provider

    # V2.3 Wire 38: 启 (Qi) 时间窗口默认 ON (内测).
    # qi_window_config 仍由 SoulFile 配置, 这里只 init container.
    # 真激活仍守门: 时间窗口 + KUN_QI_FORCE_ACTIVE 双重控制.
    if _os.getenv("KUN_QI_ENABLED", "1") == "1":
        from kun.qi.window import QiWindowConfig

        app.state.qi_window_config = QiWindowConfig()

    # V2.3 Wire 53 (C71+C72): orchestrator 装 protocol_registry + anti_gaming_detector
    # 都从 KUN_QI_RUNTIME_ENABLED block 装好的 app.state 拿; 没装 → None → 鲲行为不变.
    _protocol_registry_for_orch = getattr(app.state, "protocol_registry", None)
    _anti_gaming_for_orch: Any = None
    if _os.getenv("KUN_ANTI_GAMING_ENABLED", "1") == "1":
        try:
            from kun.security.anti_gaming import AntiGamingDetector

            _anti_gaming_for_orch = AntiGamingDetector()
        except Exception:
            pass

    # V2.1 safety singletons. KillSwitch must be created before Orchestrator
    # so the task-control API and the actual runner share one signal source.
    kill_switch = KillSwitch(sla_ms=500)
    task_timeout = TaskTimeoutGuard()

    orchestrator = Orchestrator(
        rule_engine=rule_engine,
        emergent_switch_manager=emergent_switch_manager,
        value_gate=value_gate,
        structured_step_generator=structured_step_generator,
        verification_runner=verification_runner,
        prediction_provider=pc_provider,
        model_updater=pc_updater,
        protocol_registry=_protocol_registry_for_orch,
        anti_gaming_detector=_anti_gaming_for_orch,
        decision_plane=decision_plane,
        state_ledger=state_ledger,
        hermes_adapter=hermes_adapter,
        memory_writeback=memory_writeback,
        scoring_system=scoring_system,
        kill_switch=kill_switch,
    )
    app.state.rule_engine = rule_engine
    app.state.orchestrator = orchestrator
    # V3 Mission: durable resume worker with a real Orchestrator runner. This
    # turns queued mission tasks into actual execution attempts instead of a
    # permanent "needs executor" shell.
    app.state.mission_resume_worker = MissionResumeWorker(
        runner=MissionOrchestratorRunner(orchestrator)
    )
    app.state.pending_task_resume_worker = PendingTaskResumeWorker(orchestrator)

    app.state.fast_path = FastPathRouter(
        # M3.2 暂用空 lookup, M3.3 接真 cache / template / history
        cache_lookup=None,
        template_lookup=None,
        history_lookup=None,
        deterministic_types=("tools.echo",),
        user_trust_lookup=None,  # M3.3 接 capability_card 查 task_count
    )
    app.state.kill_switch = kill_switch
    app.state.token_meter = TokenMeter()
    app.state.plan_only_gate = PlanOnlyGate()
    app.state.task_timeout = task_timeout
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


def get_incident_response(app: _AppWithState) -> IncidentResponseEngine:
    return cast(IncidentResponseEngine, app.state.incident_response)


def get_cron_scheduler(app: _AppWithState) -> CronScheduler:
    return cast(CronScheduler, app.state.cron_scheduler)


def get_mission_resume_worker(app: _AppWithState) -> MissionResumeWorker:
    return cast(MissionResumeWorker, app.state.mission_resume_worker)


def get_pending_task_resume_worker(app: _AppWithState) -> PendingTaskResumeWorker:
    return cast(PendingTaskResumeWorker, app.state.pending_task_resume_worker)


def get_value_gate(app: _AppWithState) -> ValueGate | None:
    """V2.2 §19.4: 守望主决策 gate. 默认 None, env KUN_VALUE_GATE_ENABLED=1 启用."""
    return getattr(app.state, "value_gate", None)


def get_structured_step_generator(app: _AppWithState) -> StructuredStepGenerator | None:
    """V2.2 §22: hermes 结构化执行 generator. 默认开, env KUN_HERMES_ENABLED=0 关."""
    return getattr(app.state, "structured_step_generator", None)


def get_protocol_registry_runtime(app: _AppWithState) -> Any:
    return getattr(app.state, "protocol_registry", None)


def get_pheromone_storage_runtime(app: _AppWithState) -> Any:
    return getattr(app.state, "pheromone_storage", None)


def get_qi_budget_runtime(app: _AppWithState) -> Any:
    return getattr(app.state, "qi_budget", None)


def get_capability_card_cache(app: _AppWithState) -> CapabilityCardCache | None:
    return getattr(app.state, "capability_card_cache", None)
