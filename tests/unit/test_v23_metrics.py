"""V2.3 metrics defined in kun/core/metrics.py."""

from __future__ import annotations


def test_qi_metrics_defined() -> None:
    from kun.core.metrics import qi_daily_spent_usd, qi_window_active

    assert qi_window_active._name == "kun_qi_window_active"
    assert qi_daily_spent_usd._name == "kun_qi_daily_spent_usd"


def test_protocol_metrics_defined() -> None:
    from kun.core.metrics import protocol_match_total, protocol_promotion_total

    assert protocol_match_total._name == "kun_protocol_match"
    assert protocol_promotion_total._name == "kun_protocol_promotion"


def test_predictive_coding_metric_defined() -> None:
    from kun.core.metrics import predictive_coding_error

    assert predictive_coding_error._name == "kun_predictive_coding_error"


def test_pheromone_metrics_defined() -> None:
    from kun.core.metrics import pheromone_decay_step_total, pheromone_total_strength

    assert pheromone_total_strength._name == "kun_pheromone_total_strength"
    assert pheromone_decay_step_total._name == "kun_pheromone_decay_step"


def test_anti_gaming_metric_defined() -> None:
    from kun.core.metrics import anti_gaming_detection_total

    assert anti_gaming_detection_total._name == "kun_anti_gaming_detection"


def test_capability_card_cache_metric_defined() -> None:
    from kun.core.metrics import capability_card_cache_hit_rate

    assert capability_card_cache_hit_rate._name == "kun_capability_card_cache_hit_rate"


def test_protocol_match_inc() -> None:
    from kun.core.metrics import protocol_match_total

    protocol_match_total.labels(protocol_id="x.y", hit="true").inc()


def test_anti_gaming_inc() -> None:
    from kun.core.metrics import anti_gaming_detection_total

    anti_gaming_detection_total.labels(pattern="copy_prompt").inc()


def test_protocol_promotion_inc() -> None:
    from kun.core.metrics import protocol_promotion_total

    protocol_promotion_total.labels(
        protocol_id="x.y", from_status="experimental", to_status="shadow"
    ).inc()
