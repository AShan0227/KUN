"""The audit-confirmed live production metrics stay defined (audit F158/F159).

The audit verified these 11 metrics are emitted on genuine production paths
(events outbox worker, Orchestrator.run, watchtower rule loop, LLM router) — i.e.
they are trustworthy, not test fixtures. F055 removed 4 dead metrics; this guards
the *live* ones against accidental removal, with their prometheus names pinned.
"""

from __future__ import annotations

import pytest
from kun.core import metrics

# (attribute, exported prometheus name)
_LIVE_METRICS = [
    ("llm_request_total", "kun_llm_request_total"),
    ("llm_latency_seconds", "kun_llm_latency_seconds"),
    ("llm_cost_usd", "kun_llm_cost_usd"),
    ("events_published_total", "kun_events_published_total"),
    ("events_outbox_lag", "kun_events_outbox_lag"),
    ("task_started_total", "kun_task_started_total"),
    ("task_duration_seconds", "kun_task_duration_seconds"),
    ("task_surprise_score", "kun_task_surprise_score"),
    ("watchtower_rule_latency_seconds", "kun_watchtower_rule_latency_seconds"),
    ("watchtower_intervention_rate", "kun_watchtower_intervention_rate"),
]


@pytest.mark.unit
@pytest.mark.parametrize("attr,prom_name", _LIVE_METRICS)
def test_live_metric_defined(attr: str, prom_name: str) -> None:
    metric = getattr(metrics, attr, None)
    assert metric is not None, f"confirmed-live metric {attr} is missing"
    # prometheus_client stores the registered name on _name (without _total suffix
    # for counters), so match by prefix to stay robust across client versions.
    assert getattr(metric, "_name", prom_name).replace("_total", "") in prom_name
