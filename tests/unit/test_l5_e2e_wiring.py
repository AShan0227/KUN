"""L5.5 — End-to-End wiring tests for SupervisorService + RCDH + Priority Channel.

验证:
  Supervisor.observe → cluster trigger → diagnostic_runner 调 → enrich → emit
  Priority Channel 能正确分类 cluster + promotion_timeout requests
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.supervisor.service import SupervisorService
from kun.governance.priority_channel import (
    classify_request_priority,
    prioritize_requests,
)

# ---- SupervisorService 接 diagnostic_runner ----


@pytest.mark.asyncio
async def test_supervisor_calls_diagnostic_runner_on_cluster() -> None:
    """cluster 触发时, diagnostic_runner 被调."""
    diag_calls: list[tuple[str, str, list[dict]]] = []
    received: list[dict[str, Any]] = []

    async def fake_diagnostic_runner(
        symptom: str, target_module: str, evidence: list[dict]
    ) -> dict:
        diag_calls.append((symptom, target_module, evidence))
        return {
            "diagnostic_id": "dx-fake",
            "root_cause_level": 1,
            "recommended_action": "activate",
            "scope_modules": ["llm.router"],
            "level_0_check": {"is_root_cause": False, "evidence": []},
            "level_1_check": {
                "is_root_cause": True,
                "evidence": [{"type": "feature_flag_off"}],
            },
            "level_2_check": {"is_root_cause": False, "evidence": []},
            "level_3_check": {"is_root_cause": False, "evidence": []},
        }

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(
        emitter=emitter,
        diagnostic_runner=fake_diagnostic_runner,
    )

    # 3 次 task.failed → 触发 task_failure_spike
    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    # 1 次 task.done 高 duration ratio → 触发 duration_outlier
    await svc.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "foo",
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )

    # diagnostic_runner 应该被调 (cluster 触发时)
    assert len(diag_calls) >= 1
    # cluster request 应携带 RCDH 字段
    cluster_reqs = [
        r for r in received if r.get("triggered_by") == "anomaly_cluster"
    ]
    assert len(cluster_reqs) >= 1
    cluster_req = cluster_reqs[0]
    assert cluster_req["diagnostic_id"] == "dx-fake"
    assert cluster_req["target_level_hint"] == 1
    assert cluster_req["explorer_mode_hint"] == "conservative"


@pytest.mark.asyncio
async def test_supervisor_handles_diagnostic_runner_exception() -> None:
    """diagnostic_runner 抛异常时, cluster request 仍 emit (fallback 无 enrich)."""
    received: list[dict[str, Any]] = []

    async def bad_diagnostic(
        symptom: str, target_module: str, evidence: list[dict]
    ) -> dict:
        raise RuntimeError("rcdh broken")

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(
        emitter=emitter, diagnostic_runner=bad_diagnostic
    )

    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    await svc.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "foo",
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )

    cluster_reqs = [
        r for r in received if r.get("triggered_by") == "anomaly_cluster"
    ]
    assert len(cluster_reqs) >= 1
    # 没 diagnostic 字段 (fallback 走原 cluster)
    assert "diagnostic_id" not in cluster_reqs[0]


@pytest.mark.asyncio
async def test_supervisor_without_diagnostic_runner_unchanged() -> None:
    """无 diagnostic_runner 注入时, cluster request 不变."""
    received: list[dict[str, Any]] = []

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(emitter=emitter)  # 无 diagnostic_runner
    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    await svc.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "foo",
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )
    cluster_reqs = [
        r for r in received if r.get("triggered_by") == "anomaly_cluster"
    ]
    assert len(cluster_reqs) >= 1
    assert "diagnostic_id" not in cluster_reqs[0]


# ---- Priority Channel 集成 ----


@pytest.mark.asyncio
async def test_cluster_request_classified_urgent_in_priority_channel() -> None:
    """Supervisor 产的 cluster request 应被 classify 为 urgent."""
    received: list[dict[str, Any]] = []

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(emitter=emitter)
    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    await svc.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "foo",
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )
    cluster_reqs = [
        r for r in received if r.get("triggered_by") == "anomaly_cluster"
    ]
    assert len(cluster_reqs) >= 1
    cls = classify_request_priority(cluster_reqs[0])
    assert cls.tier == "urgent"
    assert cls.boosted is True


@pytest.mark.asyncio
async def test_prioritize_requests_orders_cluster_before_normal() -> None:
    """prioritize_requests 把 cluster request 排在普通 anomaly request 之前."""
    received: list[dict[str, Any]] = []

    async def emitter(req: dict) -> None:
        received.append(req)

    svc = SupervisorService(emitter=emitter)
    # 3 次 task.failed → 触发 task_failure_spike + cluster
    for _ in range(3):
        await svc.observe(
            "task.failed", {"tenant_id": "u-test", "task_type": "foo"}
        )
    await svc.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "foo",
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )

    # 至少 1 普通 + 1 cluster
    assert len(received) >= 2
    sorted_pairs = prioritize_requests(received)
    # 第一个应该是 urgent (cluster)
    assert sorted_pairs[0][1].tier == "urgent"
    # 后续是普通 (high/medium/low — 取决于 anomaly priority)
    # task_failure_spike → high; duration_outlier → low
    assert sorted_pairs[-1][1].tier in ("high", "medium", "low")
    # urgent 严格在 non-urgent 之前
    tiers = [p[1].tier for p in sorted_pairs]
    assert tiers.index("urgent") < min(
        tiers.index(t) for t in tiers if t != "urgent"
    )
