"""Variable Registry — 决策信号源 62 变量谱 (V2.1 §17.7).

所有 StrategyMatcher 决策都从这个谱里取信号. 新增变量时统一登记,
自动接入 SignalBundle.

7 族变量:
  A 任务侧 (12) / B 用户侧 (13) / C 资源侧 (8) / D 系统侧 (10)
  E 历史侧 (8) / F 外部环境 (6) / G Meta 决策耦合 (5)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VariableFamily = Literal["task", "user", "resource", "system", "history", "env", "meta"]


@dataclass(frozen=True)
class VariableSpec:
    """单个决策信号变量规格."""

    name: str
    family: VariableFamily
    dtype: str  # e.g. "float", "int", "bool", "Literal['low','medium','high','critical']"
    description: str
    decision_uses: tuple[str, ...] = field(default_factory=tuple)


# A 族: 任务侧 (12 个)
TASK_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "task_type",
        "task",
        "str",
        "层级分类如 coding.python.fastapi",
        ("model_select", "evaluation_tier", "attention_allocation"),
    ),
    VariableSpec(
        "complexity_score",
        "task",
        "float",
        "0-1 复杂度",
        ("split_decision", "model_tier", "evaluation_escalation"),
    ),
    VariableSpec(
        "risk_level",
        "task",
        "Literal[low,medium,high,critical]",
        "全部决策点的权重锚定",
        ("all_decision_kinds",),
    ),
    VariableSpec("urgency", "task", "int", "1-5 紧迫度", ("latency_weight",)),
    VariableSpec("deadline_iso", "task", "datetime|None", "deadline", ("latency_strict",)),
    VariableSpec(
        "is_first_of_type",
        "task",
        "bool",
        "是否首次该 task_type",
        ("cold_start_escalation", "saturation_judge"),
    ),
    VariableSpec(
        "has_irreversible",
        "task",
        "bool",
        "是否含不可逆动作",
        ("plan_only_trigger", "sandbox_tier", "evaluation_tier"),
    ),
    VariableSpec(
        "involves_money",
        "task",
        "bool",
        "是否涉及金钱",
        ("approval_escalation", "plan_only_trigger"),
    ),
    VariableSpec("crosses_tenant", "task", "bool", "是否跨租户", ("sandbox_tier", "human_gate")),
    VariableSpec(
        "estimated_tokens",
        "task",
        "int",
        "预估 token 量",
        ("fork_decision", "cache_decision", "model_select"),
    ),
    VariableSpec("estimated_steps", "task", "int", "预估步数", ("hard_timeout", "ooda_frequency")),
    VariableSpec(
        "dependency_depth", "task", "int", "任务依赖图深度", ("split_granularity", "fork_decision")
    ),
)

# B 族: 用户侧 (13 个)
USER_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "audience",
        "user",
        "Literal[novice,developer,expert]",
        "回复档位",
        ("system_prompt_style", "output_depth"),
    ),
    VariableSpec(
        "trusted_models", "user", "list[(model, trust)]", "信任的模型加权", ("model_select",)
    ),
    VariableSpec("distrusted_models", "user", "list[model]", "不信任的降权", ("model_select",)),
    VariableSpec(
        "trusted_agents", "user", "list[agent_id]", "信任的外部 agent", ("fork_priority",)
    ),
    VariableSpec(
        "approval_threshold_money", "user", "float", "审批门槛金额 USD", ("plan_only_trigger",)
    ),
    VariableSpec(
        "approval_threshold_irreversible",
        "user",
        "Literal[always,never,per_action]",
        "不可逆审批策略",
        ("plan_only_trigger",),
    ),
    VariableSpec(
        "risk_tolerance", "user", "Literal[low,medium,high]", "风险容忍度", ("delta_weight",)
    ),
    VariableSpec(
        "cost_sensitivity", "user", "Literal[low,medium,high]", "成本敏感度", ("beta_weight",)
    ),
    VariableSpec(
        "speed_sensitivity", "user", "Literal[low,medium,high]", "速度敏感度", ("gamma_weight",)
    ),
    VariableSpec(
        "interruption_tolerance",
        "user",
        "Literal[low,medium,high]",
        "打扰容忍度",
        ("ask_user_trigger", "notification_channel"),
    ),
    VariableSpec(
        "evolved_traits", "user", "list[Trait]", "灵魂档案演化特征", ("multiple_decisions_prior",)
    ),
    VariableSpec(
        "current_attention_state",
        "user",
        "Literal[online,away,focus_mode]",
        "当前注意力状态",
        ("notification_channel", "ask_user_trigger"),
    ),
    VariableSpec(
        "user_role",
        "user",
        "Literal[developer,lawyer,elderly,founder,...]",
        "用户角色",
        ("global_style", "role_context"),
    ),
)

# C 族: 资源侧 (8 个)
RESOURCE_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "budget_remaining_usd", "resource", "float", "剩余预算", ("model_downgrade", "hard_cutoff")
    ),
    VariableSpec(
        "token_quota_5h_remaining",
        "resource",
        "int",
        "5h 滚动 token 配额",
        ("model_downgrade", "alert"),
    ),
    VariableSpec(
        "token_quota_daily_remaining",
        "resource",
        "int",
        "日 token 配额",
        ("model_downgrade", "alert"),
    ),
    VariableSpec(
        "token_quota_monthly_remaining",
        "resource",
        "int",
        "月 token 配额",
        ("model_downgrade", "alert"),
    ),
    VariableSpec(
        "context_tokens_used",
        "resource",
        "int",
        "已用 context",
        ("compress_decision", "summary_checkpoint"),
    ),
    VariableSpec(
        "context_window_remaining",
        "resource",
        "int",
        "剩余 context 窗口",
        ("fork_decision", "compress_decision"),
    ),
    VariableSpec(
        "running_tasks_count",
        "resource",
        "int",
        "在跑任务数",
        ("attention_guard", "queue_decision"),
    ),
    VariableSpec(
        "user_active_sessions", "resource", "int", "用户活跃 session 数", ("attention_budget",)
    ),
)

# D 族: 系统侧 (10 个)
SYSTEM_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "llm_availability",
        "system",
        "dict[model->bool]",
        "各 LLM 可用性",
        ("model_select", "fallback"),
    ),
    VariableSpec(
        "llm_current_latency_p50",
        "system",
        "dict[model->ms]",
        "实时延迟",
        ("model_select_latency_estimate",),
    ),
    VariableSpec(
        "llm_current_error_rate", "system", "dict[model->float]", "实时错误率", ("model_downgrade",)
    ),
    VariableSpec(
        "watchtower_alert_state",
        "system",
        "Literal[normal,warn,critical]",
        "守望告警",
        ("global_intervention_tier",),
    ),
    VariableSpec("online_workers", "system", "int", "在线 worker", ("multitask_concurrency",)),
    VariableSpec(
        "cache_hit_rate_permanent", "system", "float", "永久段缓存命中率", ("cache_decision",)
    ),
    VariableSpec(
        "cache_hit_rate_stable", "system", "float", "稳定段缓存命中率", ("cache_decision",)
    ),
    VariableSpec(
        "anomaly_indicator_cost", "system", "float", "成本异常", ("watchtower_escalation",)
    ),
    VariableSpec(
        "anomaly_indicator_quality", "system", "float", "质量异常", ("evaluation_escalation",)
    ),
    VariableSpec("network_latency_ms", "system", "int", "网络延迟", ("remote_api_decision",)),
)

# E 族: 历史侧 (8 个)
HISTORY_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "capability_card_success_rate",
        "history",
        "float+CI95",
        "task_type 历史 success_rate",
        ("model_select", "outcome_estimate"),
    ),
    VariableSpec(
        "capability_card_failure_modes", "history", "list", "失败模式", ("plan_only_trigger",)
    ),
    VariableSpec(
        "surprise_score_history",
        "history",
        "list[float]",
        "30d surprise 历史",
        ("evaluation_tier", "attention"),
    ),
    VariableSpec(
        "multi_judge_verdict_history",
        "history",
        "list",
        "多 judge 历史",
        ("debate_learning_curve",),
    ),
    VariableSpec(
        "user_feedback_log",
        "history",
        "累积",
        "user 显式反馈",
        ("evolution_trait", "route_preference"),
    ),
    VariableSpec("user_correction_count", "history", "int", "user 纠正次数", ("ask_user_trigger",)),
    VariableSpec(
        "similar_task_replan_count", "history", "int", "同类任务 replan 次数", ("ooda_decision",)
    ),
    VariableSpec(
        "recent_task_count", "history", "int", "最近 N 天该 task_type 次数", ("template_reuse",)
    ),
)

# F 族: 外部环境 (6 个)
ENV_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "current_time_with_tz",
        "env",
        "datetime",
        "当前时间",
        ("notification_timing", "working_hours"),
    ),
    VariableSpec(
        "is_holiday", "env", "bool", "是否节假日", ("notification_timing", "task_scheduling")
    ),
    VariableSpec(
        "user_focus_window",
        "env",
        "TimeRange",
        "用户专注时段",
        ("ask_user_trigger", "notification_downgrade"),
    ),
    VariableSpec(
        "code_freeze_window", "env", "bool", "是否代码冻结期", ("plan_only_force", "write_decision")
    ),
    VariableSpec(
        "business_seasonality", "env", "Enum", "业务季节性", ("budget_allocation", "priority")
    ),
    VariableSpec(
        "project_context_anchor",
        "env",
        "dict",
        "项目级 anchor",
        ("global_style", "system_prompt_inject"),
    ),
)

# G 族: Meta 决策耦合 (5 个)
META_VARS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "previous_decision",
        "meta",
        "dict[decision_kind, StrategyDecision]",
        "上游决策影响下游",
        ("downstream_constraint",),
    ),
    VariableSpec(
        "previous_step_outcome",
        "meta",
        "Literal[success,fail,partial]",
        "上一步结果",
        ("ooda_trigger", "debate_trigger"),
    ),
    VariableSpec(
        "in_active_experiment",
        "meta",
        "bool|str",
        "是否在 A/B 实验中",
        ("avoid_double_experiment",),
    ),
    VariableSpec("conflicting_candidates", "meta", "list", "矛盾候选", ("escalate_judge",)),
    VariableSpec("decision_chain_depth", "meta", "int", "决策嵌套深度", ("prevent_infinite_loop",)),
)


REGISTRY: dict[str, VariableSpec] = {
    spec.name: spec
    for family in (
        TASK_VARS,
        USER_VARS,
        RESOURCE_VARS,
        SYSTEM_VARS,
        HISTORY_VARS,
        ENV_VARS,
        META_VARS,
    )
    for spec in family
}


def get(name: str) -> VariableSpec:
    """取变量规格. 未知变量 KeyError."""
    return REGISTRY[name]


def list_by_family(family: VariableFamily) -> tuple[VariableSpec, ...]:
    """按族列出变量."""
    return tuple(spec for spec in REGISTRY.values() if spec.family == family)


def all_names() -> tuple[str, ...]:
    """所有变量名."""
    return tuple(REGISTRY.keys())


__all__ = [
    "ENV_VARS",
    "HISTORY_VARS",
    "META_VARS",
    "REGISTRY",
    "RESOURCE_VARS",
    "SYSTEM_VARS",
    "TASK_VARS",
    "USER_VARS",
    "VariableFamily",
    "VariableSpec",
    "all_names",
    "get",
    "list_by_family",
]
