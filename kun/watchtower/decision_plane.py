"""Watchtower decision plane — V3 system-level sparse MoE.

守望不执行任务, 只给执行层一张"策略单". 这张策略单必须被 orchestrator 消费:
- execution_mode: 影响执行模式
- context_limit: 影响 context 拉取深度
- skill_hints: 影响 TaskSpec.required_skills, 进而影响 SkillSelector
- metric_dimensions / reward_weights: 给后续评估和写回统一口径

这不是最终形态, 但已经是真接主流程的第一刀, 避免只写字段不用。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.task import ExecutionMode, TaskRef

BASE_METRICS = [
    "success_rate",
    "cost",
    "latency",
    "risk",
    "reversibility",
    "user_satisfaction",
    "surprise",
    "reuse_value",
]


def _default_context_limits() -> dict[ExecutionMode, int]:
    return {"FAST": 0, "SMART": 1, "MAX": 3, "ENSEMBLE": 3}


class StrategyPack(BaseModel):
    """一类任务的稀疏激活包."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    display_name: str
    task_type_patterns: list[str] = Field(default_factory=list)
    keyword_triggers: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)
    context_tags: list[str] = Field(default_factory=list)
    skill_hints: list[str] = Field(default_factory=list)
    metric_dimensions: list[str] = Field(default_factory=list)
    risk_watch: list[str] = Field(default_factory=list)
    default_execution_mode: ExecutionMode = "SMART"
    context_limits: dict[ExecutionMode, int] = Field(default_factory=_default_context_limits)
    reward_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "quality": 0.40,
            "user_satisfaction": 0.20,
            "cost": 0.15,
            "latency": 0.10,
            "reuse_potential": 0.10,
            "risk": 0.05,
        }
    )

    def match_score(self, task_ref: TaskRef) -> float:
        """Return a deterministic match score in [0, +inf)."""
        task_type = task_ref.meta.task_type
        text = _task_text(task_ref)
        score = 0.0
        for pattern in self.task_type_patterns:
            if fnmatch.fnmatch(task_type, pattern):
                score += 1.0 + 0.2 * pattern.count(".")
        for keyword in self.keyword_triggers:
            if keyword.lower() in text:
                score += 0.35
        return score


class WatchtowerDecision(BaseModel):
    """守望给执行层的策略单."""

    model_config = ConfigDict(extra="forbid")

    strategy_pack_id: str
    strategy_pack_name: str
    execution_mode: ExecutionMode
    context_limit: int = Field(ge=0)
    skill_hints: list[str] = Field(default_factory=list)
    metric_dimensions: list[str] = Field(default_factory=list)
    risk_watch: list[str] = Field(default_factory=list)
    reward_weights: dict[str, float] = Field(default_factory=dict)
    reason: str
    source: Literal["watchtower", "protocol", "forced"] = "watchtower"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    alert_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class WatchtowerDecisionPlane:
    """守望决策面: 选择策略包, 产出可执行策略单."""

    packs: list[StrategyPack] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.packs:
            self.packs = builtin_strategy_packs()

    def decide(
        self, task_ref: TaskRef, *, active_protocol: Any | None = None
    ) -> WatchtowerDecision:
        pack, pack_score = self._select_pack(task_ref)
        protocol_mode = _protocol_execution_mode(active_protocol)
        mode_source: Literal["watchtower", "protocol", "forced"] = "watchtower"
        if protocol_mode is not None:
            execution_mode = protocol_mode
            mode_source = "protocol"
        else:
            execution_mode = self._choose_execution_mode(task_ref, pack)

        context_limit = pack.context_limits.get(
            execution_mode,
            {"FAST": 0, "SMART": 1, "MAX": 3, "ENSEMBLE": 3}[execution_mode],
        )
        metric_dimensions = _dedupe([*BASE_METRICS, *pack.metric_dimensions])
        reward_weights = dict(pack.reward_weights)
        if active_protocol is not None:
            protocol_weights = getattr(active_protocol, "reward_weights", None)
            if isinstance(protocol_weights, dict):
                reward_weights.update(
                    {
                        str(key): float(value)
                        for key, value in protocol_weights.items()
                        if _is_number(value)
                    }
                )

        alert_flags = self._alert_flags(task_ref, pack)
        reason = (
            f"命中策略包 {pack.pack_id}; "
            f"match_score={pack_score:.2f}; "
            f"mode={execution_mode}; context_limit={context_limit}"
        )
        if mode_source == "protocol":
            reason += "; execution_mode 来自 active protocol"

        return WatchtowerDecision(
            strategy_pack_id=pack.pack_id,
            strategy_pack_name=pack.display_name,
            execution_mode=execution_mode,
            context_limit=context_limit,
            skill_hints=_dedupe(pack.skill_hints + _protocol_skill_hints(active_protocol)),
            metric_dimensions=metric_dimensions,
            risk_watch=pack.risk_watch,
            reward_weights=reward_weights,
            reason=reason,
            source=mode_source,
            confidence=min(0.95, 0.45 + min(pack_score, 2.0) * 0.25),
            alert_flags=alert_flags,
            metadata={
                "methodology_refs": pack.methodology_refs,
                "context_tags": pack.context_tags,
                "task_type": task_ref.meta.task_type,
                "risk_level": task_ref.meta.risk_level,
                "complexity_score": task_ref.meta.complexity_score,
            },
        )

    def apply(self, task_ref: TaskRef, decision: WatchtowerDecision) -> None:
        """Apply the decision to mutable execution metadata."""
        task_ref.meta.execution_mode = decision.execution_mode
        if not decision.skill_hints:
            return
        if task_ref.spec is None:
            return
        existing = list(task_ref.spec.required_skills)
        for skill_id in decision.skill_hints:
            if skill_id not in existing:
                existing.append(skill_id)
        task_ref.spec.required_skills = existing

    def _select_pack(self, task_ref: TaskRef) -> tuple[StrategyPack, float]:
        scored = [(pack, pack.match_score(task_ref)) for pack in self.packs]
        scored.sort(key=lambda item: (-item[1], item[0].pack_id))
        best, score = scored[0]
        if score <= 0:
            default = next((pack for pack in self.packs if pack.pack_id == "default"), best)
            return default, 0.0
        return best, score

    def _choose_execution_mode(self, task_ref: TaskRef, pack: StrategyPack) -> ExecutionMode:
        risk = task_ref.meta.risk_level
        complexity = task_ref.meta.complexity_score
        cost = task_ref.meta.estimated_cost_usd
        if risk == "critical" and complexity >= 0.75:
            return "ENSEMBLE"
        if risk in ("high", "critical") or complexity >= 0.72 or cost >= 1.0:
            return "MAX"
        if complexity >= 0.30:
            return "SMART"
        return "FAST" if pack.default_execution_mode == "FAST" else pack.default_execution_mode

    def _alert_flags(self, task_ref: TaskRef, pack: StrategyPack) -> list[str]:
        flags: list[str] = []
        text = _task_text(task_ref)
        # 稀疏 MoE 的偏离报警: 某类任务突然出现别的高风险语义, 守望要能看见.
        if pack.pack_id == "education" and any(
            word in text for word in ("付款", "合同", "发票", "转账", "报价")
        ):
            flags.append("education_task_contains_commercial_or_financial_terms")
        if pack.pack_id in {"education", "content"} and task_ref.meta.risk_level in {
            "high",
            "critical",
        }:
            flags.append("low_risk_domain_with_high_risk_level")
        if task_ref.meta.estimated_cost_usd > 5:
            flags.append("high_estimated_cost")
        return flags


def builtin_strategy_packs() -> list[StrategyPack]:
    """内置策略包. 后续可由 Qi 产出候选, 再经守望 promote."""
    return [
        StrategyPack(
            pack_id="default",
            display_name="通用任务",
            task_type_patterns=["*"],
            keyword_triggers=[],
            default_execution_mode="SMART",
            metric_dimensions=["general_quality", "completion_clarity"],
            risk_watch=["scope_drift", "missing_success_criteria"],
        ),
        StrategyPack(
            pack_id="education",
            display_name="教育学习",
            task_type_patterns=["education*", "learning*", "course*", "teaching*"],
            keyword_triggers=["学习", "课程", "教育", "训练", "知识点", "考试", "作业"],
            methodology_refs=["spaced_repetition", "scaffolded_learning"],
            context_tags=["education", "learning_profile", "curriculum"],
            skill_hints=["lesson_planner", "quiz_generator"],
            metric_dimensions=[
                "understanding_depth",
                "difficulty_progression",
                "knowledge_coverage",
                "review_value",
            ],
            risk_watch=["hallucinated_fact", "difficulty_jump", "missing_practice_loop"],
            default_execution_mode="SMART",
            reward_weights={
                "quality": 0.45,
                "user_satisfaction": 0.20,
                "reuse_potential": 0.15,
                "latency": 0.10,
                "cost": 0.05,
                "risk": 0.05,
            },
        ),
        StrategyPack(
            pack_id="coding",
            display_name="代码工程",
            task_type_patterns=["coding*", "code*", "software*", "dev*"],
            keyword_triggers=["代码", "测试", "bug", "接口", "重构", "mypy", "ruff", "CI"],
            methodology_refs=["read_code_first", "test_driven_patch"],
            context_tags=["repo", "tests", "architecture"],
            skill_hints=["code_reader", "code_writer", "code_reviewer"],
            metric_dimensions=[
                "test_pass_rate",
                "maintainability",
                "type_safety",
                "regression_risk",
            ],
            risk_watch=["unverified_patch", "missing_test", "destructive_command"],
            default_execution_mode="MAX",
            context_limits={"FAST": 1, "SMART": 2, "MAX": 4, "ENSEMBLE": 4},
        ),
        StrategyPack(
            pack_id="commercialization",
            display_name="商业化增长",
            task_type_patterns=["business*", "growth*", "sales*", "marketing*", "commercial*"],
            keyword_triggers=["商业化", "增长", "获客", "客户", "定价", "转化", "收入", "运营产品"],
            methodology_refs=["growth_loop", "unit_economics", "customer_development"],
            context_tags=["business", "market", "customer", "pricing"],
            skill_hints=["market_research", "pricing_planner", "outreach_planner"],
            metric_dimensions=[
                "revenue_potential",
                "customer_acquisition_likelihood",
                "conversion_path_quality",
                "external_dependency_risk",
            ],
            risk_watch=["false_market_signal", "overpromising", "unapproved_external_action"],
            default_execution_mode="MAX",
            context_limits={"FAST": 1, "SMART": 3, "MAX": 5, "ENSEMBLE": 5},
        ),
        StrategyPack(
            pack_id="product_ops",
            display_name="产品运营",
            task_type_patterns=["product*", "ops*", "operation*"],
            keyword_triggers=["留存", "活跃", "用户反馈", "产品运营", "看板", "漏斗"],
            methodology_refs=["funnel_analysis", "retention_loop"],
            context_tags=["product", "user_feedback", "metrics"],
            skill_hints=["feedback_clusterer", "metric_interpreter"],
            metric_dimensions=[
                "user_value",
                "growth_potential",
                "retention_impact",
                "execution_cost",
            ],
            risk_watch=["vanity_metric", "biased_feedback_sample"],
            default_execution_mode="SMART",
        ),
        StrategyPack(
            pack_id="external_collab",
            display_name="外部协作",
            task_type_patterns=["collab*", "email*", "partner*", "external*"],
            keyword_triggers=["发邮件", "联系", "对接", "合作", "企业", "外部 agent", "客户"],
            methodology_refs=["structured_handoff", "approval_before_side_effect"],
            context_tags=["relationship_graph", "authorization", "handoff"],
            skill_hints=["contact_planner", "approval_drafter"],
            metric_dimensions=["handoff_clarity", "approval_safety", "response_likelihood"],
            risk_watch=["unauthorized_side_effect", "privacy_leak", "wrong_recipient"],
            default_execution_mode="MAX",
        ),
        StrategyPack(
            pack_id="data_analysis",
            display_name="数据分析",
            task_type_patterns=["data*", "analytics*", "metric*", "report*"],
            keyword_triggers=["数据", "指标", "报表", "分析", "统计", "图表"],
            methodology_refs=["metric_definition_first", "outlier_check"],
            context_tags=["data", "metric", "report"],
            skill_hints=["data_profiler", "chart_builder"],
            metric_dimensions=[
                "metric_correctness",
                "data_quality",
                "outlier_handling",
                "explainability",
            ],
            risk_watch=["metric_definition_missing", "data_leak", "spurious_correlation"],
            default_execution_mode="SMART",
        ),
    ]


def _task_text(task_ref: TaskRef) -> str:
    parts = [
        task_ref.meta.task_type,
        task_ref.meta.success_criteria_short,
    ]
    if task_ref.spec is not None:
        parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.success_metrics),
                " ".join(task_ref.spec.required_skills),
                " ".join(task_ref.spec.required_tools),
                " ".join(task_ref.spec.subtasks_hint),
            ]
        )
    if task_ref.layer3_context is not None:
        parts.append(task_ref.layer3_context.summary(max_chars=600))
    return " ".join(part for part in parts if part).lower()


def _protocol_execution_mode(active_protocol: Any | None) -> ExecutionMode | None:
    if active_protocol is None:
        return None
    mode = getattr(getattr(active_protocol, "execution", None), "mode", None)
    if mode in ("FAST", "SMART", "MAX", "ENSEMBLE"):
        return cast(ExecutionMode, mode)
    return None


def _protocol_skill_hints(active_protocol: Any | None) -> list[str]:
    if active_protocol is None:
        return []
    steps = getattr(active_protocol, "skill_chain", None) or []
    out: list[str] = []
    for step in steps:
        skill = getattr(step, "skill", None)
        if isinstance(skill, str) and skill:
            out.append(skill)
    return out


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_number(value: object) -> bool:
    try:
        float(value)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


__all__ = [
    "BASE_METRICS",
    "StrategyPack",
    "WatchtowerDecision",
    "WatchtowerDecisionPlane",
    "builtin_strategy_packs",
]
