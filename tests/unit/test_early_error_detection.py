"""EarlyErrorDetector tests."""

from __future__ import annotations

from kun.engineering.early_error_detection import EarlyErrorDetector, StepObservation


def test_loop_detects_repeated_step_name() -> None:
    detector = EarlyErrorDetector(loop_threshold=3)

    signal = detector.detect_loop(
        [
            StepObservation(step_name="search"),
            StepObservation(step_name="search"),
            StepObservation(step_name="search"),
        ],
    )

    assert signal is not None
    assert signal.kind == "loop"
    assert signal.event_type == "events.early_error.loop_detected"


def test_loop_ignores_non_consecutive_steps() -> None:
    detector = EarlyErrorDetector(loop_threshold=3)

    signal = detector.detect_loop(
        [
            StepObservation(step_name="search"),
            StepObservation(step_name="edit"),
            StepObservation(step_name="search"),
        ],
    )

    assert signal is None


def test_loop_detects_repeated_dag_node() -> None:
    detector = EarlyErrorDetector(loop_threshold=2)

    signal = detector.detect_loop(
        [
            StepObservation(step_name="a", dag_node="node-1"),
            StepObservation(step_name="b", dag_node="node-1"),
        ],
    )

    assert signal is not None
    assert signal.details["dag_node"] == "node-1"


def test_scope_drift_detects_unrelated_output() -> None:
    detector = EarlyErrorDetector(drift_similarity_threshold=0.2)

    signal = detector.detect_scope_drift(
        [StepObservation(step_name="write", output_text="天气 很好 适合 出门")],
        intent_one_sentence="修复 FastAPI 登录 bug",
    )

    assert signal is not None
    assert signal.kind == "scope_drift"


def test_scope_drift_ignores_related_output() -> None:
    detector = EarlyErrorDetector(drift_similarity_threshold=0.2)

    signal = detector.detect_scope_drift(
        [StepObservation(step_name="write", output_text="修复 FastAPI 登录 bug 并补测试")],
        intent_one_sentence="修复 FastAPI 登录 bug",
    )

    assert signal is None


def test_consistency_drop_detects_decreasing_consensus() -> None:
    detector = EarlyErrorDetector(trend_window=3)

    signal = detector.detect_consistency_drop(
        [
            StepObservation(step_name="a", consensus_score=0.9),
            StepObservation(step_name="b", consensus_score=0.7),
            StepObservation(step_name="c", consensus_score=0.5),
        ],
    )

    assert signal is not None
    assert signal.kind == "consistency_drop"


def test_consistency_drop_ignores_recovery() -> None:
    detector = EarlyErrorDetector(trend_window=3)

    signal = detector.detect_consistency_drop(
        [
            StepObservation(step_name="a", consensus_score=0.9),
            StepObservation(step_name="b", consensus_score=0.7),
            StepObservation(step_name="c", consensus_score=0.8),
        ],
    )

    assert signal is None


def test_trend_detects_cost_increase() -> None:
    detector = EarlyErrorDetector(trend_window=3)

    signal = detector.detect_trend_degradation(
        [
            StepObservation(step_name="a", cost_usd=0.1),
            StepObservation(step_name="b", cost_usd=0.2),
            StepObservation(step_name="c", cost_usd=0.3),
        ],
    )

    assert signal is not None
    assert signal.details["metric"] == "cost_usd"


def test_trend_detects_quality_drop() -> None:
    detector = EarlyErrorDetector(trend_window=3)

    signal = detector.detect_trend_degradation(
        [
            StepObservation(step_name="a", quality_score=0.9),
            StepObservation(step_name="b", quality_score=0.7),
            StepObservation(step_name="c", quality_score=0.4),
        ],
    )

    assert signal is not None
    assert signal.details["metric"] == "quality_score"


def test_detect_returns_all_relevant_signals() -> None:
    detector = EarlyErrorDetector(loop_threshold=2, trend_window=3)

    signals = detector.detect(
        [
            StepObservation(
                step_name="search",
                output_text="天气 很好",
                cost_usd=0.1,
                consensus_score=0.9,
            ),
            StepObservation(
                step_name="search",
                output_text="天气 很好",
                cost_usd=0.2,
                consensus_score=0.7,
            ),
            StepObservation(
                step_name="search",
                output_text="天气 很好",
                cost_usd=0.3,
                consensus_score=0.5,
            ),
        ],
        intent_one_sentence="修复 FastAPI 登录 bug",
    )

    assert {signal.kind for signal in signals} >= {"loop", "scope_drift", "consistency_drop"}
