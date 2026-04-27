"""Prometheus metrics definitions (ADR-016).

Naming: kun.<subsystem>.<metric>.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ============== Context subsystem ==============

context_cache_hit_rate = Gauge(
    "kun_context_cache_hit_rate",
    "Prompt cache hit rate per tier (permanent/stable/semi_stable/volatile)",
    ["tier", "tenant_id"],
)

context_cache_cost_savings_usd = Counter(
    "kun_context_cache_cost_savings_usd",
    "Cumulative USD saved by prompt caching",
    ["tenant_id"],
)

# ============== LLM / Router ==============

llm_request_total = Counter(
    "kun_llm_request_total",
    "LLM requests by provider/model/role",
    ["provider", "model", "role", "tenant_id"],
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

llm_cost_runaway_total = Counter(
    "kun_llm_cost_runaway_total",
    "Tasks where actual cost > 1.2x estimated",
    ["tenant_id"],
)

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

# ============== Quality / Evaluation ==============

quality_rubric_score_p50 = Gauge(
    "kun_quality_rubric_score_p50",
    "Rubric score p50 rolling window",
    ["task_type", "tenant_id"],
)

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

# ============== KUN-Lab (V2.2 §26) ==============

lab_experiment_total = Counter(
    "kun_lab_experiment_total",
    "KUN-Lab ensemble experiments executed",
    ["task_type", "status"],  # status: ok | budget_exceeded | error
)

lab_experiment_cost_usd = Counter(
    "kun_lab_experiment_cost_usd",
    "Cumulative KUN-Lab cost (separate budget from production)",
    ["task_type"],
)

lab_experiment_latency_seconds = Histogram(
    "kun_lab_experiment_latency_seconds",
    "Ensemble total latency (max across paths)",
    ["task_type"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

lab_path_total = Counter(
    "kun_lab_path_total",
    "Individual ensemble paths run",
    ["strategy", "tier", "status"],  # status: ok | error | cancelled
)

lab_budget_cap_total = Counter(
    "kun_lab_budget_cap_total",
    "Times Wire 27 cost cap triggered (cancelled paths)",
    ["task_type"],
)

lab_promotion_total = Counter(
    "kun_lab_promotion_total",
    "RecipePromoter promotions emitted",
    ["task_type", "target_module"],
)

lab_registry_size = Gauge(
    "kun_lab_registry_size",
    "LabRecipeRegistry current entry count (lab → main repo)",
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
