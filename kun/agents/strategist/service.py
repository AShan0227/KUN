"""StrategistService — on-demand 策略搜索 (ADR-024 step 5-6).

输入: strategy_search_request (Supervisor 写, anomaly 触发)
输出: 1-3 个 StrategyExperiment 候选 → 写 runtime_experiments

第一条 RSI 实例 (L2.7): LLM 路由优化
  anomaly_kind = llm_fallback_spike → 3 个候选 (Explorer Pool 3 模式):
    Aggressive    — 提 tier (top → strong) 给该 task_type
    Conservative  — 增加 primary 重试预算, 不改路由
    Performance   — fallback provider 直升 primary

Explorer Pool 当前简化为 engineering rule-based, L3+ 闭环再上 LLM Explorer.

自指限制 (ADR-024 约束 1): target_module ∈ {strategist.*, supervisor.*,
gate.*, director.*} → 强制 requires_human_review=True, Gate 拒绝自动启用.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.agents.strategist.explorer_pool import ExplorerPoolConfig

log = get_logger("kun.agents.strategist.service")


"""自指限制 (ADR-024 §约束 1): 改 5 个监督角色之一 → 强制人审.
统一定义在 kun/governance/self_referential.py — L3.5 集中."""


@dataclass(frozen=True)
class StrategyExperiment:
    """单个候选实验, 对应 runtime_experiments 一行."""

    experiment_id: str
    target_module: str
    target_level: int  # RCDH 层 0-3
    change_spec: dict[str, Any]
    rollout_mode: str  # shadow / canary / replay / direct
    sampling_rate: float  # 0.0-1.0
    success_metric: str  # 名字 — Tester 读取的指标
    acceptance_threshold: float
    rollback_on: list[dict[str, Any]] = field(default_factory=list)
    ttl_seconds: int = 86400
    status: str = "pending"
    explorer_mode: str = "conservative"  # aggressive / conservative / performance
    requires_human_review: bool = False
    rationale: str = ""

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        """转 RuntimeExperimentRow 可消费的 dict."""
        return {
            "tenant_id": tenant_id,
            "experiment_id": self.experiment_id,
            "target_module": self.target_module,
            "target_level": self.target_level,
            "change_spec": self.change_spec,
            "rollout_mode": self.rollout_mode,
            "sampling_rate": self.sampling_rate,
            "success_metric": self.success_metric,
            "acceptance_threshold": float(self.acceptance_threshold),
            "rollback_on": self.rollback_on,
            "ttl_seconds": self.ttl_seconds,
            "status": self.status,
        }


ExperimentEmitter = Callable[[StrategyExperiment], Awaitable[None]]
"""异步 emitter — 真写 runtime_experiments 表. L3 接 DB writer; 测试用 fake."""


CapabilityHistoryReader = Callable[[str], Awaitable[list[dict[str, Any]]]]
"""读取 target_module 在最近 N 小时内的 capability 晋级记录.

Return entries shape:
    {
        "capability_id": str,
        "promoted_at": datetime,
        "enabled": bool,
        "change_summary": str,
        "metadata": dict,
    }

按时间从新到旧排序. None 表示不查 (caller 选择 forward).
"""


_BACKWARD_LOOKBACK_HOURS = 24
"""若 target_module 在该窗口内有 capability 晋级 → 考虑 backward rollback."""


def _is_self_referential(target_module: str) -> bool:
    """target_module 命中 5 个监督角色之一 → self-referential.

    Wrapper around kun.governance.self_referential.is_self_referential 保持
    本模块 backward-compat (test 直接 import); 真逻辑统一在 governance 模块.
    """
    from kun.governance.self_referential import is_self_referential

    return is_self_referential(target_module)


# ---- 第一条 RSI 实例: llm_fallback_spike ----


def _candidates_for_llm_fallback_spike(
    request: dict[str, Any],
) -> list[StrategyExperiment]:
    """anomaly_kind=llm_fallback_spike → 3 个候选 (Explorer Pool)."""
    target_module = str(request.get("target_module") or "llm.router")
    evidence = request.get("evidence") or []
    primary_provider = None
    primary_model = None
    fallback_provider = None
    for ev in evidence:
        if ev.get("primary_provider"):
            primary_provider = ev["primary_provider"]
            primary_model = ev.get("primary_model")
        if ev.get("fallback_provider"):
            fallback_provider = ev["fallback_provider"]

    candidates: list[StrategyExperiment] = []

    # Conservative — 提 tier (strong → top) 给该 task_type, sampling 30%
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=1,  # L1 (activation) — 改 router 配置
            change_spec={
                "kind": "tier_upgrade",
                "from_tier": "strong",
                "to_tier": "top",
                "task_types": ["*"],
            },
            rollout_mode="canary",
            sampling_rate=0.3,
            success_metric="llm_fallback_rate",
            acceptance_threshold=0.05,  # 目标 fallback < 5%
            rollback_on=[
                {
                    "metric": "cost_usd_per_task",
                    "operator": ">",
                    "value": 0.5,
                },
                {
                    "metric": "task_failure_rate",
                    "operator": ">",
                    "value": 0.1,
                },
            ],
            explorer_mode="conservative",
            rationale=(
                f"primary={primary_provider}/{primary_model} 频繁 fallback "
                f"→ 提 tier 减少 fallback, canary 30% 观察."
            ),
        )
    )

    # Aggressive — fallback provider 直升 primary, sampling 50%
    if fallback_provider:
        candidates.append(
            StrategyExperiment(
                experiment_id=new_id("experiment_run"),
                target_module=target_module,
                target_level=1,
                change_spec={
                    "kind": "primary_swap",
                    "old_primary": primary_provider,
                    "new_primary": fallback_provider,
                },
                rollout_mode="canary",
                sampling_rate=0.5,
                success_metric="task_success_rate",
                acceptance_threshold=0.9,
                rollback_on=[
                    {
                        "metric": "task_success_rate",
                        "operator": "<",
                        "value": 0.7,
                    },
                ],
                explorer_mode="aggressive",
                rationale=(
                    f"fallback={fallback_provider} 命中率高 "
                    f"→ 升 primary, 看是否更稳."
                ),
            )
        )

    # Performance — 增加 primary 重试预算, 不改路由
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=1,
            change_spec={
                "kind": "retry_budget_increase",
                "from_retries": 1,
                "to_retries": 3,
            },
            rollout_mode="shadow",  # shadow 不影响生产
            sampling_rate=1.0,
            success_metric="llm_fallback_rate",
            acceptance_threshold=0.1,
            rollback_on=[
                {
                    "metric": "latency_p95_ms",
                    "operator": ">",
                    "value": 5000,
                },
            ],
            explorer_mode="performance",
            rationale=(
                "primary 可能只是临时抖动 → 重试 3 次, shadow 验证不引入 P95 增长."
            ),
        )
    )

    return candidates


def _candidates_for_task_failure_spike(
    request: dict[str, Any],
) -> list[StrategyExperiment]:
    """anomaly_kind=task_failure_spike → 单 Conservative 候选."""
    target_module = str(request.get("target_module") or "executor.unknown")
    evidence = request.get("evidence") or []
    task_type = "unknown"
    for ev in evidence:
        if ev.get("task_type"):
            task_type = ev["task_type"]
            break

    return [
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=2,  # 模块层
            change_spec={
                "kind": "tier_upgrade",
                "from_tier": "strong",
                "to_tier": "top",
                "task_types": [task_type],
            },
            rollout_mode="canary",
            sampling_rate=0.2,
            success_metric="task_success_rate",
            acceptance_threshold=0.85,
            rollback_on=[
                {"metric": "task_success_rate", "operator": "<", "value": 0.5},
            ],
            explorer_mode="conservative",
            rationale=(
                f"task_type={task_type} 失败率上升 → 临时提 tier 看是否模型能力问题."
            ),
        )
    ]


def _candidates_for_context_oversized_spike(
    request: dict[str, Any],
) -> list[StrategyExperiment]:
    """L3.1 第二条 RSI 实例: context 压缩策略.

    anomaly_kind=context_oversized_spike → 3 个候选 (Explorer Pool):
      Conservative — 老消息 summary 压缩 (保语义, 加预处理 step)
      Aggressive   — 硬截断 (drop messages older than N turns)
      Performance  — RAG 替代全 context (检索相关历史, 不传所有)
    """
    target_module = str(request.get("target_module") or "llm.context")
    evidence = request.get("evidence") or []
    threshold_tokens = 80_000
    latest_input_tokens = 0
    for ev in evidence:
        if ev.get("threshold_tokens"):
            threshold_tokens = int(ev["threshold_tokens"])
        if ev.get("latest_input_tokens"):
            latest_input_tokens = int(ev["latest_input_tokens"])

    candidates: list[StrategyExperiment] = []

    # Conservative — summary 压缩 (保语义)
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=2,  # 模块层 — 改 context assembler
            change_spec={
                "kind": "context_summary_compression",
                "older_than_turns": 10,
                "summary_target_tokens": 500,
                "trigger_input_tokens": threshold_tokens,
            },
            rollout_mode="canary",
            sampling_rate=0.3,
            success_metric="avg_input_tokens_per_call",
            acceptance_threshold=float(threshold_tokens) * 0.6,
            rollback_on=[
                {"metric": "task_success_rate", "operator": "<", "value": 0.85},
                {"metric": "context_loss_complaint_rate", "operator": ">", "value": 0.05},
            ],
            explorer_mode="conservative",
            rationale=(
                f"input_tokens 频繁超 {threshold_tokens} (latest={latest_input_tokens}) "
                f"→ 用 summary 压缩老消息, 保语义."
            ),
        )
    )

    # Aggressive — 硬截断
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=2,
            change_spec={
                "kind": "context_hard_truncation",
                "max_turns": 20,
                "preserve_top_pins": True,  # 保留 anchor 顶部 pinning
            },
            rollout_mode="canary",
            sampling_rate=0.2,
            success_metric="avg_input_tokens_per_call",
            acceptance_threshold=float(threshold_tokens) * 0.4,
            rollback_on=[
                {"metric": "task_success_rate", "operator": "<", "value": 0.8},
            ],
            explorer_mode="aggressive",
            rationale=(
                "input_tokens 严重超阈 → 硬截断保留最近 20 轮 + anchor pinning. "
                "风险: 丢中间上下文."
            ),
        )
    )

    # Performance — RAG 替代全 context
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=2,
            change_spec={
                "kind": "context_rag_retrieval",
                "top_k": 8,
                "embedding_model": "default",
                "fallback_to_truncation": True,
            },
            rollout_mode="shadow",  # shadow 不影响生产
            sampling_rate=1.0,
            success_metric="avg_input_tokens_per_call",
            acceptance_threshold=float(threshold_tokens) * 0.3,
            rollback_on=[
                {"metric": "retrieval_relevance_score", "operator": "<", "value": 0.6},
                {"metric": "latency_p95_ms", "operator": ">", "value": 3000},
            ],
            explorer_mode="performance",
            rationale=(
                "改 RAG 检索相关历史而不传全部. shadow 验证检索相关度不掉."
            ),
        )
    )

    return candidates


def _candidates_for_skill_mismatch_spike(
    request: dict[str, Any],
) -> list[StrategyExperiment]:
    """L3.2 第三条 RSI 实例: skill 选择启发式.

    anomaly_kind=skill_mismatch_spike → 3 个候选 (Explorer Pool):
      Conservative — alternative skill 替换 (skill_id_swap), canary 30%
      Aggressive   — task_type split (细分 sub-types), 长期方案
      Performance  — capability_card 加权 (调小 cold-start damping)
    """
    target_module = str(request.get("target_module") or "skill.unknown")
    evidence = request.get("evidence") or []
    task_type = "unknown"
    skill_id = "unknown"
    failure_rate = 0.0
    sample_size = 0
    for ev in evidence:
        if ev.get("task_type"):
            task_type = ev["task_type"]
        if ev.get("skill_id"):
            skill_id = ev["skill_id"]
        if ev.get("failure_rate"):
            failure_rate = float(ev["failure_rate"])
        if ev.get("sample_size"):
            sample_size = int(ev["sample_size"])

    candidates: list[StrategyExperiment] = []

    # Conservative — alternative skill 替换
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=1,  # activation 层 — 改 skill 路由
            change_spec={
                "kind": "skill_id_swap",
                "task_type": task_type,
                "old_skill_id": skill_id,
                "selection_strategy": "next_best_by_capability_card",
            },
            rollout_mode="canary",
            sampling_rate=0.3,
            success_metric="skill_task_success_rate",
            acceptance_threshold=1.0 - failure_rate * 0.5,  # 目标至少减半失败
            rollback_on=[
                {"metric": "skill_task_success_rate", "operator": "<", "value": 0.6},
            ],
            explorer_mode="conservative",
            rationale=(
                f"({task_type}, {skill_id}) 失败率 {failure_rate:.0%} "
                f"(n={sample_size}) → 换 next_best capability_card 推荐."
            ),
        )
    )

    # Aggressive — task_type 拆分
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module=target_module,
            target_level=0,  # 设计层 — task_type 分类是产品决策
            change_spec={
                "kind": "task_type_split",
                "task_type": task_type,
                "split_rationale": (
                    "frequent failure suggests task_type is too coarse — split"
                ),
                "requires_director_assistance": True,
            },
            rollout_mode="shadow",
            sampling_rate=1.0,
            success_metric="skill_task_success_rate",
            acceptance_threshold=0.85,
            rollback_on=[
                {"metric": "skill_task_success_rate", "operator": "<", "value": 0.7},
            ],
            explorer_mode="aggressive",
            rationale=(
                f"({task_type}) 类型可能太粗 → 拆 sub-types 各配 skill. "
                f"shadow 验证, Director 协助拆分定义."
            ),
        )
    )

    # Performance — capability_card 加权 (调 damping)
    candidates.append(
        StrategyExperiment(
            experiment_id=new_id("experiment_run"),
            target_module="kun/interface/llm/capability_router",
            target_level=2,  # 模块层 — 改路由 damping 参数
            change_spec={
                "kind": "capability_damping_tweak",
                "from_damping_denominator": 30,
                "to_damping_denominator": 15,  # damping 更弱, 让历史信号更快显现
                "applies_to_skill": skill_id,
            },
            rollout_mode="canary",
            sampling_rate=0.2,
            success_metric="skill_routing_change_lag_calls",
            acceptance_threshold=10.0,  # 路由改变需要 ≤ 10 次 calls 显现
            rollback_on=[
                {"metric": "skill_task_success_rate", "operator": "<", "value": 0.6},
            ],
            explorer_mode="performance",
            rationale=(
                f"capability_card cold-start damping (n/30) 太慢, "
                f"调到 n/15 让 ({skill_id}) 失败信号更快反映到路由."
            ),
        )
    )

    return candidates


_CANDIDATE_GENERATORS: dict[
    str, Callable[[dict[str, Any]], list[StrategyExperiment]]
] = {
    "llm_fallback_spike": _candidates_for_llm_fallback_spike,
    "task_failure_spike": _candidates_for_task_failure_spike,
    "context_oversized_spike": _candidates_for_context_oversized_spike,
    "skill_mismatch_spike": _candidates_for_skill_mismatch_spike,
}


# ---- L3.3 Forward / Backward 双修复策略 ----


def _candidate_for_backward_rollback(
    request: dict[str, Any],
    capability_to_rollback: dict[str, Any],
) -> StrategyExperiment:
    """构造 backward rollback 候选 — 单实验, 不走 Explorer Pool.

    把 target_module 的最近一次 enabled capability 标 disabled + 准备回滚.
    """
    target_module = str(request.get("target_module") or "unknown")
    cap_id = capability_to_rollback.get("capability_id", "unknown")
    change_summary = capability_to_rollback.get("change_summary", "")
    return StrategyExperiment(
        experiment_id=new_id("experiment_run"),
        target_module=target_module,
        target_level=1,  # activation 层 — 禁用 capability
        change_spec={
            "kind": "capability_rollback",
            "rollback_capability_id": cap_id,
            "original_change_summary": change_summary,
            "direction": "backward",
        },
        rollout_mode="direct",  # 回滚不 canary, 直接关
        sampling_rate=1.0,
        success_metric=request.get("evidence", [{}])[0].get(
            "type", "anomaly_rate"
        ),
        acceptance_threshold=0.5,  # 异常率减半即视为成功
        rollback_on=[
            # "回滚的回滚" — 如果异常率反而升 → re-enable capability
            {"metric": "anomaly_rate", "operator": ">", "value": 1.2},
        ],
        explorer_mode="backward",
        rationale=(
            f"target_module={target_module} 最近 24h 有 capability "
            f"{cap_id} 启用 ('{change_summary[:60]}'), 异常窗口与之相关 → "
            f"backward rollback 比 forward 新探索更安全."
        ),
    )


def select_repair_direction(
    request: dict[str, Any],
    capability_history: list[dict[str, Any]] | None,
    *,
    lookback_hours: int = _BACKWARD_LOOKBACK_HOURS,
) -> str:
    """决定 repair direction: 'forward' (新探索) 或 'backward' (回滚).

    Engineering 规则:
      - capability_history 空 / None → forward (无可回滚)
      - 最近 lookback_hours 内有 enabled capability 命中 target_module → backward
      - 否则 → forward (无近期改动可疑)
    """
    if not capability_history:
        return "forward"
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    for entry in capability_history:
        promoted_at = entry.get("promoted_at")
        enabled = entry.get("enabled", False)
        if not enabled:
            continue
        if isinstance(promoted_at, datetime) and promoted_at >= cutoff:
            return "backward"
        if isinstance(promoted_at, str):
            # ISO 字符串容错
            try:
                parsed = datetime.fromisoformat(promoted_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if parsed >= cutoff:
                    return "backward"
            except ValueError:
                continue
    return "forward"


def _most_recent_enabled_capability(
    capability_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """从 capability_history 取最近一条 enabled 的."""
    for entry in capability_history:
        if entry.get("enabled"):
            return entry
    return None


class StrategistService:
    """on-demand Strategist 服务.

    输入: strategy_search_request dict
    输出: 1-3 个 StrategyExperiment 候选; emitter 异步落 runtime_experiments.
    """

    def __init__(
        self,
        *,
        emitter: ExperimentEmitter | None = None,
        capability_history_reader: CapabilityHistoryReader | None = None,
        explorer_pool_config: ExplorerPoolConfig | None = None,
    ) -> None:
        from kun.agents.strategist.explorer_pool import (
            load_explorer_pool_config,
        )

        self._emitter = emitter
        self._history_reader = capability_history_reader
        self._explorer_pool: ExplorerPoolConfig = (
            explorer_pool_config or load_explorer_pool_config()
        )

    async def propose_candidates(
        self,
        request: dict[str, Any],
    ) -> list[StrategyExperiment]:
        """读 request → 生成候选 → emitter 落库.

        Forward / Backward auto-select (L3.3):
          - 若 capability_history_reader 注入 + 最近 24h target_module 有 enabled
            capability → backward rollback (单实验, 不 Explorer Pool)
          - 否则 → forward (Explorer Pool 3 模式)

        未知 anomaly_kind → 空 list (Supervisor 升级到 LLM Strategist 或人).
        """
        anomaly_kind = request.get("anomaly_kind") or ""
        target_module = str(request.get("target_module") or "")

        # Forward / Backward 决策
        capability_history: list[dict[str, Any]] | None = None
        if self._history_reader is not None and target_module:
            try:
                capability_history = await self._history_reader(target_module)
            except Exception as e:
                log.warning(
                    "strategist.history_reader_failed",
                    error=str(e),
                    target_module=target_module,
                )
                capability_history = None

        direction = select_repair_direction(request, capability_history)
        if direction == "backward":
            entry = (
                _most_recent_enabled_capability(capability_history)
                if capability_history
                else None
            )
            if entry is not None:
                candidates = [_candidate_for_backward_rollback(request, entry)]
                log.info(
                    "strategist.backward_rollback_selected",
                    target_module=target_module,
                    capability_id=entry.get("capability_id"),
                )
                return await self._emit_and_adjust(candidates, anomaly_kind)

        # Forward — Explorer Pool
        generator = _CANDIDATE_GENERATORS.get(anomaly_kind)
        if generator is None:
            log.warning(
                "strategist.unknown_anomaly_kind",
                anomaly_kind=anomaly_kind,
                target_module=request.get("target_module"),
            )
            return []

        candidates = generator(request)
        # L4.1 Explorer Pool 过滤 — 仅保留 enabled forward modes
        candidates = self._explorer_pool.filter_candidates(candidates)
        return await self._emit_and_adjust(candidates, anomaly_kind)

    async def _emit_and_adjust(
        self,
        candidates: list[StrategyExperiment],
        anomaly_kind: str,
    ) -> list[StrategyExperiment]:
        """共用: 自指标 human review + 强制 target_level=0 + emit 落库.

        L3.5 自指限制强化:
          - requires_human_review=True (Gate 不 auto-admit)
          - status='awaiting_human_review'
          - target_level=0 (设计层 — 改自己属于设计决策)
          - rationale 标 [SELF-REFERENTIAL: forced design-level review]
        """
        from dataclasses import replace

        adjusted: list[StrategyExperiment] = []
        for c in candidates:
            if _is_self_referential(c.target_module):
                adjusted.append(
                    replace(
                        c,
                        requires_human_review=True,
                        status="awaiting_human_review",
                        target_level=0,  # L3.5: 强制设计层
                        rationale=(
                            c.rationale
                            + " [SELF-REFERENTIAL: forced target_level=0 design-level human review]"
                        ),
                    )
                )
            else:
                adjusted.append(c)

        for c in adjusted:
            if self._emitter is not None:
                try:
                    await self._emitter(c)
                except Exception as e:
                    log.warning(
                        "strategist.emitter_failed",
                        error=str(e),
                        experiment_id=c.experiment_id,
                    )

        log.info(
            "strategist.candidates_proposed",
            anomaly_kind=anomaly_kind,
            count=len(adjusted),
            self_referential=sum(1 for c in adjusted if c.requires_human_review),
            backward=sum(1 for c in adjusted if c.explorer_mode == "backward"),
        )
        return adjusted


def experiment_as_dict(exp: StrategyExperiment) -> dict[str, Any]:
    """Helper: StrategyExperiment → plain dict (for downstream consumers)."""
    return asdict(exp)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CapabilityHistoryReader",
    "ExperimentEmitter",
    "StrategistService",
    "StrategyExperiment",
    "experiment_as_dict",
    "select_repair_direction",
]
