"""V3 unified strategy scoring.

The scorecard is intentionally small.  It turns the same 6-8 practical metrics
into one object that routing, capability writeback, NUO, and memory can share.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kun.datamodel.runtime import RuntimeState
from kun.datamodel.task import TaskRef


class StrategyScorecard(BaseModel):
    """Unified V3 scorecard."""

    task_id: str
    strategy_pack_id: str = "default"
    overall: float = Field(ge=0.0, le=1.0)
    metrics: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    reason: str = ""


@dataclass
class UnifiedScoringSystem:
    """Compute a compact scorecard from real runtime signals."""

    def score_task(
        self,
        *,
        task_ref: TaskRef,
        runtime: RuntimeState,
        status: str,
        validation_outcome: str,
        validation_score: float | None,
        surprise_score: float,
        decision: Any = None,
        user_satisfaction: float | None = None,
    ) -> StrategyScorecard:
        strategy_pack_id = str(getattr(decision, "strategy_pack_id", "default"))
        reward_weights = dict(getattr(decision, "reward_weights", {}) or {})
        weights = _normalize_weights(
            {
                "success_rate": reward_weights.get("success_rate", 0.32),
                "cost": reward_weights.get("cost", 0.14),
                "latency": reward_weights.get("latency", 0.10),
                "risk": reward_weights.get("risk", 0.12),
                "reversibility": reward_weights.get("reversibility", 0.08),
                "user_satisfaction": reward_weights.get("user_satisfaction", 0.10),
                "reuse_value": reward_weights.get("reuse_value", 0.08),
                "surprise": reward_weights.get("surprise", 0.06),
            }
        )
        success = _success_metric(status=status, validation_outcome=validation_outcome)
        if validation_score is not None:
            success = (success + _clamp(validation_score)) / 2.0
        cost = _cost_metric(
            actual=runtime.accumulated_cost_usd_equivalent,
            estimated=task_ref.meta.estimated_cost_usd,
        )
        latency = _latency_metric(
            actual=sum(step.duration_sec for step in runtime.completed_steps),
            estimated=task_ref.meta.estimated_duration_sec,
        )
        risk = _risk_metric(task_ref.meta.risk_level, status=status)
        reversibility = 1.0 if task_ref.meta.risk_level in {"low", "medium"} else 0.4
        satisfaction = 0.5 if user_satisfaction is None else _clamp(user_satisfaction)
        reuse = _reuse_metric(task_ref)
        surprise = _surprise_metric(surprise_score)
        metrics = {
            "success_rate": success,
            "cost": cost,
            "latency": latency,
            "risk": risk,
            "reversibility": reversibility,
            "user_satisfaction": satisfaction,
            "reuse_value": reuse,
            "surprise": surprise,
        }
        overall = sum(metrics[name] * weights.get(name, 0.0) for name in metrics)
        return StrategyScorecard(
            task_id=task_ref.meta.task_id,
            strategy_pack_id=strategy_pack_id,
            overall=_clamp(overall),
            metrics=metrics,
            weights=weights,
            reason=(
                f"strategy={strategy_pack_id}; success={success:.2f}; "
                f"cost={cost:.2f}; risk={risk:.2f}; surprise={surprise:.2f}"
            ),
        )


def _success_metric(*, status: str, validation_outcome: str) -> float:
    if status != "done":
        return 0.0
    return {"pass": 1.0, "partial": 0.55, "fail": 0.0}.get(validation_outcome, 0.5)


def _cost_metric(*, actual: float, estimated: float) -> float:
    if estimated <= 0:
        return 0.8 if actual <= 0.05 else 0.5
    ratio = actual / max(estimated, 0.0001)
    if ratio <= 0.75:
        return 1.0
    if ratio <= 1.0:
        return 0.85
    if ratio <= 1.5:
        return 0.55
    return 0.2


def _latency_metric(*, actual: float, estimated: float) -> float:
    if estimated <= 0 or actual <= 0:
        return 0.7
    ratio = actual / max(estimated, 0.0001)
    if ratio <= 1.0:
        return 1.0
    if ratio <= 2.0:
        return 0.6
    return 0.3


def _risk_metric(risk_level: str, *, status: str) -> float:
    base = {"low": 1.0, "medium": 0.82, "high": 0.55, "critical": 0.35}.get(risk_level, 0.6)
    return base if status == "done" else min(base, 0.25)


def _reuse_metric(task_ref: TaskRef) -> float:
    if task_ref.spec is None:
        return 0.4
    signals = (
        len(task_ref.spec.required_skills)
        + len(task_ref.spec.success_metrics)
        + len(task_ref.spec.subtasks_hint)
    )
    return _clamp(0.35 + min(signals, 8) * 0.08)


def _surprise_metric(surprise_score: float) -> float:
    # Surprise is useful when it is positive but too much means unstable.
    score = _clamp(surprise_score)
    if score <= 0.35:
        return 0.7
    if score <= 0.7:
        return 1.0
    return 0.55


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {key: max(float(value), 0.0) for key, value in weights.items()}
    total = sum(cleaned.values()) or 1.0
    return {key: value / total for key, value in cleaned.items()}


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


__all__ = ["StrategyScorecard", "UnifiedScoringSystem"]
