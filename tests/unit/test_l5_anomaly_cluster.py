"""L5.1 — Anomaly Cluster 单测."""

from __future__ import annotations

import pytest
from kun.agents.supervisor.anomaly_cluster import (
    _module_prefix,
    cluster_anomalies,
    cluster_to_search_request,
)
from kun.agents.supervisor.service import SupervisorService


def _req(
    *,
    request_id: str,
    tenant_id: str = "u-test",
    anomaly_kind: str,
    target_module: str,
    evidence: list[dict] | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "anomaly_kind": anomaly_kind,
        "target_module": target_module,
        "evidence": evidence or [{"kind": anomaly_kind}],
        "priority": "medium",
    }


# ---- _module_prefix ----


def test_prefix_dot_path() -> None:
    assert _module_prefix("executor.coding.refactor") == "executor"


def test_prefix_slash_path() -> None:
    assert _module_prefix("kun/agents/executor") == "kun"


def test_prefix_bare_name() -> None:
    assert _module_prefix("executor") == "executor"


# ---- cluster_anomalies — Rule 1 (module systemic) ----


def test_no_cluster_when_single_request() -> None:
    requests = [_req(request_id="r1", anomaly_kind="X", target_module="m1")]
    assert cluster_anomalies(requests) == []


def test_no_cluster_when_unrelated() -> None:
    requests = [
        _req(request_id="r1", anomaly_kind="X", target_module="m1"),
        _req(request_id="r2", anomaly_kind="Y", target_module="m2"),
    ]
    # X / Y 不同 anomaly_kind 且 m1 / m2 没相同 prefix
    out = cluster_anomalies(requests)
    assert out == []


def test_module_systemic_cluster_same_module_multiple_kinds() -> None:
    """同 target_module 上 2 个不同 anomaly_kind → module_systemic cluster."""
    requests = [
        _req(request_id="r1", anomaly_kind="llm_fallback_spike", target_module="llm.router"),
        _req(request_id="r2", anomaly_kind="duration_outlier", target_module="llm.router"),
    ]
    clusters = cluster_anomalies(requests)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "module_systemic"
    assert c.primary_target_module == "llm.router"
    assert set(c.anomaly_kinds) == {"llm_fallback_spike", "duration_outlier"}
    assert set(c.member_request_ids) == {"r1", "r2"}
    assert c.priority == "high"


# ---- Rule 2 (cross-module pattern) ----


def test_cross_module_cluster_same_kind_multi_modules() -> None:
    """同 anomaly_kind + 2+ target_module 前缀相同 → cross_module_pattern."""
    requests = [
        _req(request_id="r1", anomaly_kind="task_failure_spike", target_module="executor.coding"),
        _req(request_id="r2", anomaly_kind="task_failure_spike", target_module="executor.refactor"),
    ]
    clusters = cluster_anomalies(requests)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "cross_module_pattern"
    assert "executor" in c.primary_target_module
    assert set(c.target_modules) == {"executor.coding", "executor.refactor"}


def test_cross_module_not_fired_when_prefix_differs() -> None:
    """target_module 前缀不同 → 不形成 cross_module cluster."""
    requests = [
        _req(request_id="r1", anomaly_kind="task_failure_spike", target_module="executor.coding"),
        _req(request_id="r2", anomaly_kind="task_failure_spike", target_module="skill.bug_fix"),
    ]
    clusters = cluster_anomalies(requests)
    # 仅 1 个不同 prefix, 不形成 cross_module 在默认阈值 2
    assert clusters == []


# ---- Rule 3 (tenant degradation) ----


def test_tenant_degradation_cluster_3_plus_independent_anomalies() -> None:
    """同 tenant + 3+ 独立 anomaly (没被前 2 条 rules 抓) → tenant_degradation."""
    # 3 个独立 anomaly: 不同 kind + 不同 module + 不同 prefix
    requests = [
        _req(request_id="r1", anomaly_kind="llm_fallback_spike", target_module="llm.router"),
        _req(request_id="r2", anomaly_kind="task_failure_spike", target_module="executor.x"),
        _req(request_id="r3", anomaly_kind="skill_mismatch_spike", target_module="skill.bug_fix"),
    ]
    clusters = cluster_anomalies(requests)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "tenant_degradation"
    assert clusters[0].primary_target_module == "<tenant-wide>"


def test_tenant_isolation_no_cross_tenant_cluster() -> None:
    requests = [
        _req(request_id="r1", tenant_id="u-a", anomaly_kind="X", target_module="m"),
        _req(request_id="r2", tenant_id="u-a", anomaly_kind="Y", target_module="m"),
        _req(request_id="r3", tenant_id="u-b", anomaly_kind="X", target_module="m"),
    ]
    clusters = cluster_anomalies(requests)
    # u-a 的两条形成 module_systemic; u-b 单条不入任何 cluster
    assert len(clusters) == 1
    assert clusters[0].tenant_id == "u-a"


# ---- 优先级 + 不重复 ----


def test_member_not_reused_across_rules() -> None:
    """已形成 Rule 1 cluster 的 request 不再进 Rule 2 / 3."""
    requests = [
        _req(request_id="r1", anomaly_kind="X", target_module="executor.a"),
        _req(request_id="r2", anomaly_kind="Y", target_module="executor.a"),
        _req(request_id="r3", anomaly_kind="X", target_module="executor.b"),
    ]
    clusters = cluster_anomalies(requests)
    # Rule 1: r1+r2 (同 executor.a, X+Y)
    # Rule 2: r3 单条不形成 cross_module
    # r1 已 seen, 不进 Rule 2
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "module_systemic"


def test_cluster_combined_evidence_preserves_source() -> None:
    requests = [
        _req(
            request_id="r1",
            anomaly_kind="X",
            target_module="m",
            evidence=[{"k": "v1"}],
        ),
        _req(
            request_id="r2",
            anomaly_kind="Y",
            target_module="m",
            evidence=[{"k": "v2"}],
        ),
    ]
    cluster = cluster_anomalies(requests)[0]
    # combined_evidence 应保留 from_request_id 让审计追溯
    sources = {e["_from_request_id"] for e in cluster.combined_evidence}
    assert sources == {"r1", "r2"}


# ---- cluster_to_search_request ----


def test_cluster_to_search_request_shape() -> None:
    requests = [
        _req(request_id="r1", anomaly_kind="X", target_module="llm.router"),
        _req(request_id="r2", anomaly_kind="Y", target_module="llm.router"),
    ]
    cluster = cluster_anomalies(requests)[0]
    payload = cluster_to_search_request(cluster)
    assert payload["triggered_by"] == "anomaly_cluster"
    assert payload["anomaly_kind"] == "module_systemic"
    assert payload["priority"] == "high"
    assert payload["severity"] == "strong"
    assert payload["target_module"] == "llm.router"
    assert "cluster_member_request_ids" in payload
    assert set(payload["cluster_member_request_ids"]) == {"r1", "r2"}
    assert set(payload["cluster_anomaly_kinds"]) == {"X", "Y"}


def test_cluster_to_search_request_has_dedup_key() -> None:
    requests = [
        _req(request_id="r1", anomaly_kind="X", target_module="m"),
        _req(request_id="r2", anomaly_kind="Y", target_module="m"),
    ]
    cluster = cluster_anomalies(requests)[0]
    payload = cluster_to_search_request(cluster)
    assert "cluster" in payload["dedup_key"]
    assert "module_systemic" in payload["dedup_key"]


# ---- SupervisorService integration: wired cluster emit ----


@pytest.mark.asyncio
async def test_supervisor_emits_cluster_request_when_module_systemic() -> None:
    """同 target_module 上 2 个不同 anomaly_kind → 单独的 cluster request emit."""
    received: list[dict] = []

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(emitter=emitter)
    # 3 次 task.failed (task_type=foo) — 触发 task_failure_spike
    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    # 6 次 task.done with high duration ratio — 触发 duration_outlier
    for _ in range(1):
        await svc.observe(
            "task.done",
            {
                "tenant_id": "u-test",
                "task_type": "foo",
                "duration_sec": 10.0,
                "avg_duration_sec_for_task_type": 1.0,
            },
        )
    # 两个 anomaly_kind 同 target_module=executor.foo → cluster
    cluster_reqs = [r for r in received if r.get("triggered_by") == "anomaly_cluster"]
    # 至少 1 cluster
    assert len(cluster_reqs) >= 1
    c = cluster_reqs[0]
    assert c["anomaly_kind"] == "module_systemic"
    assert c["priority"] == "high"
    assert c["severity"] == "strong"
