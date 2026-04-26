"""StrategyMatcher — 统一所有"按场景选最佳策略"决策 (V2.1 §17 / ADR-019).

核心:
  - SignalBundle 统一信号源 (62 变量谱, kun.core.variable_registry)
  - StrategyCandidate 统一候选格式
  - strategy_score = α·成果 - β·代价 - γ·延迟 - δ·风险 (按 risk × user 动态权重)
  - 决策点之间有依赖关系 (previous_decisions 传递)
  - 反馈回写 (capability_card / playbook / surprise_score)

18+ 决策点 enumerate_candidates 走同一抽象, 走"工程化 + LLM 混合"模式.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id

logger = logging.getLogger(__name__)


DecisionKind = Literal[
    "model_select",
    "judge_trigger",
    "ask_user_trigger",
    "fork_decision",
    "plan_only_trigger",
    "compress_decision",
    "sandbox_tier",
    "evaluation_tier",
    "approval_threshold",
    "context_preheat_depth",
    "skill_dispatch",
    "tool_choose",
    "retry_policy",
    "rollback_trigger",
    "escalation_level",
    "cache_decision",
    "experiment_branch",
    "notification_channel",
    # V2.1 扩展 (decision_kind_registry 热加载)
    "fast_path_check",
    "preflight_module_run",
    "emergent_switch",
    "panorama_tier",
]

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass
class Weights:
    """strategy_score 权重 (按 risk × user 动态)."""

    alpha: float = 0.4  # 成果权重
    beta: float = 0.3  # 代价权重
    gamma: float = 0.2  # 延迟权重
    delta: float = 0.1  # 风险权重

    def normalize(self) -> Weights:
        total = self.alpha + self.beta + self.gamma + self.delta
        if total <= 0:
            return Weights()
        return Weights(
            alpha=self.alpha / total,
            beta=self.beta / total,
            gamma=self.gamma / total,
            delta=self.delta / total,
        )


# 默认权重表 (按 risk_level 锚定)
WEIGHT_TABLE: dict[RiskLevel, Weights] = {
    "low": Weights(alpha=0.30, beta=0.30, gamma=0.40, delta=0.00),
    "medium": Weights(alpha=0.40, beta=0.30, gamma=0.20, delta=0.10),
    "high": Weights(alpha=0.55, beta=0.15, gamma=0.15, delta=0.15),
    "critical": Weights(alpha=0.70, beta=0.05, gamma=0.10, delta=0.15),
}


class SignalBundle(BaseModel):
    """决策信号包. 来自 62 变量谱 (kun.core.variable_registry)."""

    model_config = {"arbitrary_types_allowed": True}

    task: dict[str, Any] = Field(default_factory=dict)
    user: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)
    system: dict[str, Any] = Field(default_factory=dict)
    history: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    def get(self, var_name: str, default: Any = None) -> Any:
        """跨族取变量值. 不存在返 default (并标记 prior_used)."""
        for family in ("task", "user", "resource", "system", "history", "env", "meta"):
            d = getattr(self, family)
            if var_name in d:
                return d[var_name]
        return default

    def get_risk_level(self) -> RiskLevel:
        risk = self.task.get("risk_level", "medium")
        if risk not in ("low", "medium", "high", "critical"):
            return "medium"
        return risk  # type: ignore[no-any-return]


class StrategyCandidate(BaseModel):
    """单个候选策略."""

    candidate_id: str
    description: str
    expected_outcome: float = Field(ge=0.0, le=1.0)
    expected_cost_usd: float = Field(ge=0.0)
    expected_latency_sec: float = Field(ge=0.0)
    risk_penalty: float = Field(ge=0.0, le=1.0, default=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoredCandidate(BaseModel):
    """打分后的候选."""

    candidate: StrategyCandidate
    score: float
    score_breakdown: dict[str, float]


class StrategyDecision(BaseModel):
    """决策结果."""

    decision_id: str = Field(default_factory=lambda: new_id("sd"))
    decision_kind: DecisionKind
    chosen: StrategyCandidate
    runners_up: list[ScoredCandidate] = Field(default_factory=list)
    score_breakdown: dict[str, float]
    weights_used: dict[str, float]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    fast_path_skipped: bool = False
    llm_assist_mode: Literal["none", "augment", "decide", "metacognitive"] = "none"
    signals_summary: dict[str, Any] = Field(default_factory=dict)


# ---- candidate enumerator registry ----

CandidateEnumerator = Callable[
    [SignalBundle, dict[DecisionKind, StrategyDecision]], Awaitable[list[StrategyCandidate]]
]


class StrategyMatcher:
    """主入口. 所有决策点走它."""

    def __init__(self) -> None:
        self._enumerators: dict[DecisionKind, CandidateEnumerator] = {}
        self._writeback_hooks: list[
            Callable[[StrategyDecision, dict[str, Any]], Awaitable[None]]
        ] = []

    def register(self, kind: DecisionKind, enum: CandidateEnumerator) -> None:
        """注册决策点的候选枚举器."""
        self._enumerators[kind] = enum

    def register_writeback(
        self,
        hook: Callable[[StrategyDecision, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """注册反馈回写钩子. (实时 / 聚合 / 学习三档分别注册)"""
        self._writeback_hooks.append(hook)

    def compute_weights(self, signals: SignalBundle) -> Weights:
        """按 risk × user_preference 算 α/β/γ/δ."""
        risk = signals.get_risk_level()
        base = Weights(
            alpha=WEIGHT_TABLE[risk].alpha,
            beta=WEIGHT_TABLE[risk].beta,
            gamma=WEIGHT_TABLE[risk].gamma,
            delta=WEIGHT_TABLE[risk].delta,
        )

        # 用户偏好叠加
        user = signals.user
        if user.get("cost_sensitivity") == "high":
            base.beta += 0.10
        if user.get("speed_sensitivity") == "high":
            base.gamma += 0.10
        if user.get("risk_tolerance") == "low":
            base.delta += 0.10

        # 资源紧张 → β 升
        if signals.resource.get("budget_remaining_usd", 100.0) < 1.0:
            base.beta += 0.15

        # 紧迫度高 → γ 升
        if signals.task.get("urgency", 1) >= 4:
            base.gamma += 0.10

        # critical 任务 cap (α 锁定不能 < 0.5)
        normalized = base.normalize()
        if risk == "critical" and normalized.alpha < 0.5:
            # 强制拉回 critical 锚定
            return WEIGHT_TABLE["critical"]
        return normalized

    def score(
        self,
        candidate: StrategyCandidate,
        weights: Weights,
    ) -> ScoredCandidate:
        """对单个候选打分."""
        # 归一化各维度 (cost / latency 不天然 0-1, 用 1 - sigmoid 投射)
        outcome_term = weights.alpha * candidate.expected_outcome
        # 代价: 假设 1 USD 是高代价基准
        cost_norm = min(candidate.expected_cost_usd / 1.0, 1.0)
        cost_term = weights.beta * cost_norm
        # 延迟: 60s 是高延迟基准
        latency_norm = min(candidate.expected_latency_sec / 60.0, 1.0)
        latency_term = weights.gamma * latency_norm
        risk_term = weights.delta * candidate.risk_penalty

        score_value = outcome_term - cost_term - latency_term - risk_term

        return ScoredCandidate(
            candidate=candidate,
            score=score_value,
            score_breakdown={
                "outcome_term": outcome_term,
                "cost_term": -cost_term,
                "latency_term": -latency_term,
                "risk_term": -risk_term,
            },
        )

    async def decide(
        self,
        kind: DecisionKind,
        signals: SignalBundle,
        previous_decisions: dict[DecisionKind, StrategyDecision] | None = None,
    ) -> StrategyDecision:
        """主决策入口."""
        prev = previous_decisions or {}
        if kind not in self._enumerators:
            raise ValueError(f"No enumerator registered for decision_kind={kind}")

        candidates = await self._enumerators[kind](signals, prev)
        if not candidates:
            raise RuntimeError(
                f"enumerate_candidates returned empty for {kind}. "
                "All decision points must produce ≥1 candidate (fallback)."
            )

        weights = self.compute_weights(signals)
        scored = sorted(
            (self.score(c, weights) for c in candidates),
            key=lambda s: s.score,
            reverse=True,
        )

        chosen = scored[0]
        decision = StrategyDecision(
            decision_kind=kind,
            chosen=chosen.candidate,
            runners_up=scored[1:],
            score_breakdown=chosen.score_breakdown,
            weights_used={
                "alpha": weights.alpha,
                "beta": weights.beta,
                "gamma": weights.gamma,
                "delta": weights.delta,
            },
            signals_summary={
                "risk_level": signals.get_risk_level(),
                "complexity": signals.task.get("complexity_score", 0.0),
                "task_type": signals.task.get("task_type", "unknown"),
            },
        )
        return decision

    async def writeback(
        self,
        decision: StrategyDecision,
        actual_outcome: float | None = None,
        actual_cost_usd: float | None = None,
        actual_latency_sec: float | None = None,
    ) -> None:
        """反馈回写. 走所有注册的钩子 (实时/聚合/学习)."""
        info = {
            "actual_outcome": actual_outcome,
            "actual_cost_usd": actual_cost_usd,
            "actual_latency_sec": actual_latency_sec,
        }
        for hook in self._writeback_hooks:
            try:
                await hook(decision, info)
            except Exception:
                logger.exception("writeback hook failed (non-fatal)")


# ---- module-level singleton ----

_matcher: StrategyMatcher | None = None


def get_matcher() -> StrategyMatcher:
    """获取全局 StrategyMatcher (单例)."""
    global _matcher
    if _matcher is None:
        _matcher = StrategyMatcher()
    return _matcher


def reset_matcher() -> None:
    """测试用. 重置单例."""
    global _matcher
    _matcher = None


__all__ = [
    "WEIGHT_TABLE",
    "CandidateEnumerator",
    "DecisionKind",
    "RiskLevel",
    "ScoredCandidate",
    "SignalBundle",
    "StrategyCandidate",
    "StrategyDecision",
    "StrategyMatcher",
    "Weights",
    "get_matcher",
    "reset_matcher",
]
