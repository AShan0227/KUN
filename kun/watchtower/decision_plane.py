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

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.datamodel.task import ExecutionMode, TaskRef
from kun.engineering.credit_assignment import get_contribution_tracker
from kun.engineering.memory_invocation_policy import decide_memory_invocation_for_task
from kun.memory.similar_task_recall import (
    SimilarTaskExperience,
    summarize_strategy_votes,
)

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


def _default_reward_weights() -> dict[str, float]:
    return {
        "quality": 0.40,
        "user_satisfaction": 0.20,
        "cost": 0.15,
        "latency": 0.10,
        "reuse_potential": 0.10,
        "risk": 0.05,
    }


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
    reward_weights: dict[str, float] = Field(default_factory=_default_reward_weights)

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
        self,
        task_ref: TaskRef,
        *,
        active_protocol: Any | None = None,
        mission_strategy: dict[str, Any] | None = None,
        similar_experiences: list[SimilarTaskExperience] | None = None,
        shadow_packs: list[StrategyPack] | None = None,
    ) -> WatchtowerDecision:
        similar_experiences = similar_experiences or []
        strategy_votes = summarize_strategy_votes(similar_experiences)
        pack, pack_score = self._select_pack(
            task_ref,
            similar_experiences=similar_experiences,
            strategy_votes=strategy_votes,
        )
        protocol_mode = _protocol_execution_mode(active_protocol)
        mission_adjustment = _mission_strategy_adjustment(mission_strategy)
        mode_source: Literal["watchtower", "protocol", "forced"] = "watchtower"
        if protocol_mode is not None:
            execution_mode = protocol_mode
            mode_source = "protocol"
        else:
            execution_mode = self._choose_execution_mode(task_ref, pack)
        if mission_adjustment.min_execution_mode is not None:
            execution_mode = _max_execution_mode(
                execution_mode, mission_adjustment.min_execution_mode
            )

        context_limit = pack.context_limits.get(
            execution_mode,
            {"FAST": 0, "SMART": 1, "MAX": 3, "ENSEMBLE": 3}[execution_mode],
        )
        metric_dimensions = _dedupe(
            [*BASE_METRICS, *pack.metric_dimensions, *mission_adjustment.metric_dimensions]
        )
        process_skill_hints = _skill_hints_from_process_experiences(similar_experiences)
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
        _apply_reward_boosts(reward_weights, mission_adjustment.reward_weight_boosts)

        alert_flags = _dedupe([*self._alert_flags(task_ref, pack), *mission_adjustment.alert_flags])
        memory_invocation = decide_memory_invocation_for_task(
            task_ref,
            strategy_pack=pack,
        )
        memory_policy = memory_invocation.to_memory_policy_ticket()
        reason = (
            f"命中策略包 {pack.pack_id}; "
            f"match_score={pack_score:.2f}; "
            f"mode={execution_mode}; context_limit={context_limit}"
        )
        if mode_source == "protocol":
            reason += "; execution_mode 来自 active protocol"
        if mission_adjustment.reason:
            reason += f"; mission_review={mission_adjustment.reason}"
        if strategy_votes:
            top_vote = next(iter(strategy_votes.items()))
            reason += f"; similar_experience={top_vote[0]}:{top_vote[1]:.2f}"
        if process_skill_hints:
            reason += f"; process_skill_hints={','.join(process_skill_hints[:3])}"
        shadow_matches = _shadow_pack_matches(
            task_ref,
            shadow_packs or [],
            live_pack_id=pack.pack_id,
            live_pack_score=pack_score,
        )

        return WatchtowerDecision(
            strategy_pack_id=pack.pack_id,
            strategy_pack_name=pack.display_name,
            execution_mode=execution_mode,
            context_limit=context_limit,
            skill_hints=_dedupe(
                pack.skill_hints + process_skill_hints + _protocol_skill_hints(active_protocol)
            ),
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
                "mission_review_adjustments": mission_adjustment.model_dump(mode="json"),
                "similar_experience_count": len(similar_experiences),
                "similar_experience_refs": [
                    {
                        "asset_id": item.asset_id,
                        "memory_layer": item.memory_layer,
                        "task_type": item.task_type,
                        "strategy_pack_id": item.strategy_pack_id,
                        "validation_outcome": item.validation_outcome,
                        "score_overall": item.score_overall,
                        "similarity_score": item.similarity_score,
                        "positive_weight": item.positive_weight,
                        "reason": item.reason,
                    }
                    for item in similar_experiences[:5]
                ],
                "similar_strategy_votes": strategy_votes,
                "process_experience_skill_hints": process_skill_hints,
                "memory_invocation_policy": memory_invocation.model_dump(mode="json"),
                "memory_policy": memory_policy.model_dump(mode="json"),
                "qi_shadow_strategy_candidates": shadow_matches,
                "qi_shadow_candidate_count": len(shadow_matches),
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

    def _select_pack(
        self,
        task_ref: TaskRef,
        *,
        similar_experiences: list[SimilarTaskExperience] | None = None,
        strategy_votes: dict[str, float] | None = None,
    ) -> tuple[StrategyPack, float]:
        scored = [
            (
                pack,
                _pack_base_score(pack, task_ref)
                + _strategy_credit_bonus(pack.pack_id, tenant_id=_tenant_id_from_task(task_ref))
                + _similar_experience_bonus(
                    pack.pack_id,
                    similar_experiences=similar_experiences or [],
                    strategy_votes=strategy_votes or {},
                ),
            )
            for pack in self.packs
        ]
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


async def load_qi_shadow_strategy_packs(
    *,
    tenant_id: str,
    store: AssetStore | None = None,
    limit: int = 1000,
) -> list[StrategyPack]:
    """Load reviewed Qi strategy drafts as shadow-only Watchtower candidates.

    These packs are never inserted into ``self.packs`` and never affect the
    returned decision directly.  They are sidecar candidates so Watchtower can
    observe "what Qi would have chosen" before any human/canary promotion.
    """

    store = store or get_store()
    assets = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=limit)
    out: list[StrategyPack] = []
    for asset in assets:
        pack = _qi_shadow_pack_from_asset(asset)
        if pack is not None:
            out.append(pack)
    return out


def _qi_shadow_pack_from_asset(asset: LayeredAsset) -> StrategyPack | None:
    metadata = asset.l1_metadata
    if metadata.get("source") != "qi.idle_replay.strategy_pack_draft":
        return None
    if metadata.get("qi_review_status") != "ready_for_human_review":
        return None
    if metadata.get("qi_rollout_plan_status") != "shadow_plan":
        return None
    if metadata.get("production_action") is not False:
        return None
    draft = _as_dict(metadata.get("strategy_pack_draft"))
    if not draft:
        return None
    proposed_pack_id = str(
        metadata.get("proposed_pack_id") or draft.get("proposed_pack_id") or ""
    ).strip()
    if not proposed_pack_id:
        return None
    return StrategyPack(
        pack_id=f"qi_shadow:{proposed_pack_id}",
        display_name=f"启影子候选: {draft.get('display_name') or proposed_pack_id!s}",
        task_type_patterns=_string_list(draft.get("task_type_patterns")),
        keyword_triggers=_string_list(draft.get("keyword_triggers")),
        methodology_refs=_string_list(draft.get("methodology_refs")),
        context_tags=_string_list(draft.get("context_tags")),
        skill_hints=_string_list(draft.get("skill_hints")),
        metric_dimensions=_string_list(draft.get("metric_dimensions")),
        risk_watch=_string_list(draft.get("risk_watch")),
        default_execution_mode=_execution_mode_or_smart(draft.get("default_execution_mode")),
        context_limits=_context_limits_from_draft(draft.get("context_limits")),
        reward_weights=_reward_weights_from_draft(draft.get("reward_weights")),
    )


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


def _shadow_pack_matches(
    task_ref: TaskRef,
    packs: list[StrategyPack],
    *,
    live_pack_id: str,
    live_pack_score: float,
    limit: int = 3,
) -> list[dict[str, Any]]:
    matches: list[tuple[StrategyPack, float]] = [
        (pack, pack.match_score(task_ref)) for pack in packs
    ]
    matches = [(pack, score) for pack, score in matches if score > 0]
    matches.sort(key=lambda item: (-item[1], item[0].pack_id))
    out: list[dict[str, Any]] = []
    for pack, score in matches[:limit]:
        out.append(
            {
                "pack_id": pack.pack_id,
                "strategy_pack_name": pack.display_name,
                "match_score": round(score, 4),
                "would_outscore_live": score > live_pack_score,
                "live_pack_id": live_pack_id,
                "live_pack_score": round(live_pack_score, 4),
                "would_execution_mode": pack.default_execution_mode,
                "skill_hints": pack.skill_hints[:5],
                "metric_dimensions": pack.metric_dimensions[:8],
                "risk_watch": pack.risk_watch[:8],
                "shadow_only": True,
                "production_action": False,
            }
        )
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _execution_mode_or_smart(value: Any) -> ExecutionMode:
    text = str(value or "SMART").strip().upper()
    if text in {"FAST", "SMART", "MAX", "ENSEMBLE"}:
        return cast(ExecutionMode, text)
    return "SMART"


def _context_limits_from_draft(value: Any) -> dict[ExecutionMode, int]:
    defaults = _default_context_limits()
    if not isinstance(value, dict):
        return defaults
    out = dict(defaults)
    for key, raw in value.items():
        mode = _execution_mode_or_smart(key)
        try:
            out[mode] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _reward_weights_from_draft(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return _default_reward_weights()
    out: dict[str, float] = {}
    for key, raw in value.items():
        try:
            out[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return out or _default_reward_weights()


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


def _pack_base_score(pack: StrategyPack, task_ref: TaskRef) -> float:
    if pack.pack_id == "default":
        return 0.0
    return pack.match_score(task_ref)


def _strategy_credit_bonus(pack_id: str, *, tenant_id: str | None = None) -> float:
    """Hot MoE feedback: historically useful strategy packs get a small boost.

    The base keyword/task-type match still dominates.  Credit only breaks ties
    or nudges similar packs, which keeps simple deterministic routing stable.
    """
    try:
        score = get_contribution_tracker().contribution_score(
            pack_id,
            "strategy_pack",
            tenant_id=tenant_id,
        )
    except Exception:
        return 0.0
    return min(0.35, max(0.0, score) * 0.35)


def _tenant_id_from_task(task_ref: TaskRef) -> str | None:
    owner = getattr(task_ref.meta, "owner", None)
    tenant_id = getattr(owner, "tenant_id", None) if owner is not None else None
    if tenant_id:
        tenant_text = str(tenant_id)
        return tenant_text
    try:
        from kun.core.tenancy import current_tenant

        tenant_text = str(current_tenant().tenant_id)
        return tenant_text
    except Exception:
        return None


def _similar_experience_bonus(
    pack_id: str,
    *,
    similar_experiences: list[SimilarTaskExperience],
    strategy_votes: dict[str, float],
) -> float:
    """Cold-start MoE feedback from actual memory assets.

    Durable contribution credit is still the stronger signal.  Similar memories
    are a short-loop nudge: if past similar tasks repeatedly succeeded with a
    strategy pack, let it influence ambiguous routing immediately.
    """

    if not similar_experiences:
        return 0.0
    vote = float(strategy_votes.get(pack_id, 0.0))
    if vote <= 0:
        return 0.0
    return min(0.45, vote * 0.35)


def _skill_hints_from_process_experiences(
    experiences: list[SimilarTaskExperience],
    *,
    limit: int = 3,
) -> list[str]:
    """Let proven execution-process memory nudge the next task's skill path.

    This closes the small but important loop: execution-process memory should
    not only be shown in the prompt; if a similar past task succeeded with a
    concrete skill, Watchtower can pass that skill hint to Orchestrator. Failed
    or weakly related experiences stay out.
    """

    candidates = [
        experience
        for experience in experiences
        if experience.memory_layer == "execution_process"
        and experience.skill_used
        and experience.positive_weight > 0
    ]
    candidates.sort(
        key=lambda item: (
            -item.positive_weight,
            -item.similarity_score,
            item.step_id if item.step_id is not None else 9999,
            item.asset_id,
        )
    )
    return _dedupe([item.skill_used or "" for item in candidates])[: max(0, limit)]


class MissionStrategyAdjustment(BaseModel):
    """Small mission-review signal consumed by Watchtower.

    Mission review should not blindly override execution.  It nudges sparse
    MoE scoring: budget review makes cost more visible, risk review makes risk
    more visible, and unstable missions avoid FAST mode.
    """

    model_config = ConfigDict(extra="forbid")

    reward_weight_boosts: dict[str, float] = Field(default_factory=dict)
    metric_dimensions: list[str] = Field(default_factory=list)
    alert_flags: list[str] = Field(default_factory=list)
    min_execution_mode: ExecutionMode | None = None
    reason: str = ""


def _mission_strategy_adjustment(
    mission_strategy: dict[str, Any] | None,
) -> MissionStrategyAdjustment:
    if not isinstance(mission_strategy, dict):
        return MissionStrategyAdjustment()
    last_review = mission_strategy.get("last_review")
    if not isinstance(last_review, dict):
        return MissionStrategyAdjustment()

    summary = str(last_review.get("summary") or "").strip()
    budget_notes = str(last_review.get("budget_notes") or "").strip()
    risk_notes = str(last_review.get("risk_notes") or "").strip()
    reason_parts: list[str] = []
    boosts: dict[str, float] = {}
    dimensions: list[str] = []
    flags: list[str] = []
    min_mode: ExecutionMode | None = None

    if budget_notes:
        boosts["cost"] = 0.12
        boosts["budget_adherence"] = 0.10
        dimensions.append("budget_adherence")
        flags.append("mission_review_budget_attention")
        reason_parts.append("budget")
        if _contains_any(budget_notes, ("超预算", "超支", "burn", "expensive", "cost")):
            min_mode = "SMART"

    if risk_notes:
        boosts["risk"] = 0.14
        boosts["reversibility"] = 0.08
        dimensions.append("risk_followup")
        flags.append("mission_review_risk_attention")
        reason_parts.append("risk")
        min_mode = _max_execution_mode(min_mode or "FAST", "SMART")
        if _contains_any(risk_notes, ("高风险", "不可逆", "合规", "安全", "越权", "critical")):
            min_mode = _max_execution_mode(min_mode, "MAX")

    review_text = " ".join(part for part in (summary, budget_notes, risk_notes) if part)
    if _contains_any(review_text, ("不确定", "分歧", "失败", "卡住", "异常", "反复")):
        boosts["success_rate"] = 0.08
        dimensions.append("recovery_confidence")
        flags.append("mission_review_uncertainty_attention")
        min_mode = _max_execution_mode(min_mode or "FAST", "SMART")
        reason_parts.append("uncertainty")

    return MissionStrategyAdjustment(
        reward_weight_boosts=boosts,
        metric_dimensions=_dedupe(dimensions),
        alert_flags=_dedupe(flags),
        min_execution_mode=min_mode,
        reason="+".join(reason_parts),
    )


def _apply_reward_boosts(weights: dict[str, float], boosts: dict[str, float]) -> None:
    for key, boost in boosts.items():
        current = float(weights.get(key, 0.0))
        weights[key] = round(min(0.75, max(current, current + float(boost))), 4)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


_MODE_RANK: dict[ExecutionMode, int] = {"FAST": 0, "SMART": 1, "MAX": 2, "ENSEMBLE": 3}


def _max_execution_mode(left: ExecutionMode, right: ExecutionMode) -> ExecutionMode:
    return left if _MODE_RANK[left] >= _MODE_RANK[right] else right


__all__ = [
    "BASE_METRICS",
    "MissionStrategyAdjustment",
    "StrategyPack",
    "WatchtowerDecision",
    "WatchtowerDecisionPlane",
    "builtin_strategy_packs",
    "load_qi_shadow_strategy_packs",
]
