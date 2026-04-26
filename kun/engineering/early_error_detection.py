"""早期错误左移检测 (BATCH4 C10 / T25)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal

EarlyErrorKind = Literal["loop", "scope_drift", "consistency_drop", "trend_degradation"]


@dataclass(frozen=True)
class StepObservation:
    step_name: str
    output_text: str = ""
    dag_node: str | None = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    quality_score: float | None = None
    consensus_score: float | None = None


@dataclass(frozen=True)
class EarlyErrorSignal:
    kind: EarlyErrorKind
    event_type: str
    severity: Literal["warn", "error"] = "warn"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class EarlyErrorDetector:
    """检测执行中是否已经开始走偏, 让守望系统尽早介入."""

    def __init__(
        self,
        *,
        loop_threshold: int = 3,
        drift_similarity_threshold: float = 0.15,
        trend_window: int = 3,
    ) -> None:
        self.loop_threshold = loop_threshold
        self.drift_similarity_threshold = drift_similarity_threshold
        self.trend_window = trend_window

    def detect(
        self,
        observations: list[StepObservation],
        *,
        intent_one_sentence: str,
    ) -> list[EarlyErrorSignal]:
        signals: list[EarlyErrorSignal] = []
        loop = self.detect_loop(observations)
        if loop is not None:
            signals.append(loop)

        drift = self.detect_scope_drift(observations, intent_one_sentence=intent_one_sentence)
        if drift is not None:
            signals.append(drift)

        consistency = self.detect_consistency_drop(observations)
        if consistency is not None:
            signals.append(consistency)

        trend = self.detect_trend_degradation(observations)
        if trend is not None:
            signals.append(trend)
        return signals

    def detect_loop(self, observations: list[StepObservation]) -> EarlyErrorSignal | None:
        if len(observations) < self.loop_threshold:
            return None

        recent = observations[-self.loop_threshold :]
        step_names = [item.step_name for item in recent]
        dag_nodes = [item.dag_node for item in recent if item.dag_node]
        if len(set(step_names)) == 1:
            return EarlyErrorSignal(
                kind="loop",
                event_type="events.early_error.loop_detected",
                message="同一个 step 连续重复, 疑似死循环。",
                details={"step_name": step_names[-1], "repeat_count": self.loop_threshold},
            )
        if len(dag_nodes) == self.loop_threshold and len(set(dag_nodes)) == 1:
            return EarlyErrorSignal(
                kind="loop",
                event_type="events.early_error.loop_detected",
                message="同一个 DAG 节点连续重复访问, 疑似死循环。",
                details={"dag_node": dag_nodes[-1], "repeat_count": self.loop_threshold},
            )
        return None

    def detect_scope_drift(
        self,
        observations: list[StepObservation],
        *,
        intent_one_sentence: str,
    ) -> EarlyErrorSignal | None:
        if not observations:
            return None
        latest = observations[-1]
        similarity = _jaccard_similarity(intent_one_sentence, latest.output_text)
        if latest.output_text and similarity < self.drift_similarity_threshold:
            return EarlyErrorSignal(
                kind="scope_drift",
                event_type="events.early_error.scope_drift",
                message="当前输出和原始意图相似度过低, 可能范围漂移。",
                details={"similarity": similarity, "threshold": self.drift_similarity_threshold},
            )
        return None

    def detect_consistency_drop(
        self,
        observations: list[StepObservation],
    ) -> EarlyErrorSignal | None:
        scores = [item.consensus_score for item in observations if item.consensus_score is not None]
        if len(scores) < self.trend_window:
            return None
        recent = scores[-self.trend_window :]
        if _strictly_decreasing(recent):
            return EarlyErrorSignal(
                kind="consistency_drop",
                event_type="events.early_error.consistency_drop",
                message="多判官一致性连续下降。",
                details={"recent_scores": recent},
            )
        return None

    def detect_trend_degradation(
        self,
        observations: list[StepObservation],
    ) -> EarlyErrorSignal | None:
        if len(observations) < self.trend_window:
            return None
        recent = observations[-self.trend_window :]
        costs = [item.cost_usd for item in recent]
        latencies = [item.latency_ms for item in recent]
        qualities = [item.quality_score for item in recent if item.quality_score is not None]

        if _strictly_increasing(costs):
            return _trend_signal("cost_usd", costs)
        if _strictly_increasing(latencies):
            return _trend_signal("latency_ms", latencies)
        if len(qualities) == self.trend_window and _strictly_decreasing(qualities):
            return _trend_signal("quality_score", qualities)
        return None


def _trend_signal(metric: str, values: list[float]) -> EarlyErrorSignal:
    return EarlyErrorSignal(
        kind="trend_degradation",
        event_type="events.early_error.trend_degradation",
        message=f"{metric} 连续恶化。",
        details={"metric": metric, "recent_values": values},
    )


def _strictly_increasing(values: list[float]) -> bool:
    return len(values) >= 2 and all(left < right for left, right in pairwise(values))


def _strictly_decreasing(values: list[float]) -> bool:
    return len(values) >= 2 and all(left > right for left, right in pairwise(values))


def _jaccard_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", text) if token.strip()}
