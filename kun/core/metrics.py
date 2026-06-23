"""Prometheus metrics definitions (ADR-016).

Naming: kun.<subsystem>.<metric>.

Cardinality rules:
  - `tenant_id` is permitted ONLY on cost / quality / security counters that
    must be billed/audited per tenant. Request-rate / latency / cache counters
    must NOT carry tenant_id (N tenants × M models × K roles explodes the
    time-series count). Per-tenant ops dashboards should aggregate from logs
    + traces, not from metrics.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Context-cache metrics (context_cache_hit_rate / context_cache_cost_savings_usd)
# were defined but never emitted — the prompt-cache runtime layer isn't wired, so
# they only created empty /metrics series (false-green dashboards). Removed
# (audit F055); re-add each WITH an emit point when that subsystem lands (F055a).

# ============== LLM / Router ==============

llm_request_total = Counter(
    "kun_llm_request_total",
    "LLM requests by provider/model/role",
    ["provider", "model", "role"],
)

llm_latency_seconds = Histogram(
    "kun_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

llm_cost_usd = Counter(
    "kun_llm_cost_usd",
    "Cumulative LLM cost",
    ["provider", "model", "tenant_id"],
)

llm_fallback_total = Counter(
    "kun_llm_fallback_total",
    "Times LLM router fell back to next tier",
    ["from_provider", "to_provider", "reason"],
)

# llm_cost_runaway_total removed (audit F055): defined but never emitted — the
# "actual cost > 1.2x estimated" overrun detection was never wired (and the
# budget kill switch is a different signal). Re-add with a real emit point when
# cost-overrun detection is implemented (F055a).

# ============== Watchtower ==============

watchtower_intervention_rate = Counter(
    "kun_watchtower_intervention_total",
    "Watchtower interventions by severity",
    ["severity", "rule_id", "tenant_id"],
)

watchtower_rule_latency_seconds = Histogram(
    "kun_watchtower_rule_latency_seconds",
    "Rule evaluation latency",
    ["rule_id"],
)

# quality_rubric_score_p50 removed (audit F055): a p50 rolling-window gauge with
# no producer (nothing computes/sets it). Re-add with a real emit point when
# rubric-score aggregation is wired (F055a).

# ============== Tenancy / Security ==============

tenant_cross_access_attempt = Counter(
    "kun_tenant_cross_access_attempt_total",
    "Cross-tenant access attempts (CRITICAL alert)",
    ["from_tenant", "to_tenant"],
)

# ============== Events / Outbox ==============

events_outbox_lag = Gauge(
    "kun_events_outbox_lag",
    "Unpublished events in outbox (higher = NATS behind)",
)

events_published_total = Counter(
    "kun_events_published_total",
    "Events published to NATS",
    ["event_type"],
)

# ============== Task lifecycle ==============

task_started_total = Counter(
    "kun_task_started_total",
    "Tasks started",
    ["tenant_id", "task_type"],
)

task_duration_seconds = Histogram(
    "kun_task_duration_seconds",
    "End-to-end task duration",
    ["task_type", "status"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)

task_surprise_score = Histogram(
    "kun_task_surprise_score",
    "Task surprise scores (ADR-015)",
    ["task_type"],
    buckets=(0.1, 0.3, 0.5, 0.6, 0.8, 1.0),
)
