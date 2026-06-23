"""No dead Prometheus metrics (audit F055).

Metrics defined but never emitted create empty /metrics series and false-green
dashboards. These were removed; this guards against re-introducing them without
a real emit point (re-add WITH emit when their feature lands — see F055a).
"""

from __future__ import annotations

import pytest
from kun.core import metrics


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "context_cache_hit_rate",
        "context_cache_cost_savings_usd",
        "llm_cost_runaway_total",
        "quality_rubric_score_p50",
    ],
)
def test_unbacked_metrics_removed(name: str) -> None:
    assert not hasattr(metrics, name), (
        f"{name} was re-added without an emit point — wire a producer or keep it out"
    )


@pytest.mark.unit
def test_emitted_metrics_still_present() -> None:
    # Metrics that DO have emit sites must remain.
    for name in (
        "llm_request_total",
        "llm_latency_seconds",
        "llm_cost_usd",
        "llm_fallback_total",
        "watchtower_intervention_rate",
        "task_started_total",
        "events_published_total",
        "tenant_cross_access_attempt",
    ):
        assert hasattr(metrics, name), f"{name} missing"
