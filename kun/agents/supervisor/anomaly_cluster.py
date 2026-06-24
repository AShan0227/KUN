"""Anomaly Cluster — 多 anomaly 自动聚类为系统性问题 (L5.1, ADR-024 §自创任务).

L5 §交付标志: "监督线发现的系统性问题 → 自动转 Strategist 任务".
工程化实现 = 看到多个相关 anomaly → 聚成 cluster → 生成高优 strategy_search_request.

聚类规则 (engineering, 不调 LLM):

  Rule 1 — 同 target_module + 多种 anomaly_kind 在窗口内
    → "module-level systemic issue"; 优先级升级

  Rule 2 — 同 anomaly_kind + 多 target_module (前缀相关)
    → "cross-module pattern"; 提示设计层问题

  Rule 3 — 同 tenant + ≥ N (默认 3) 独立 anomaly 在窗口内
    → "tenant degradation"; 标 high priority

cluster 输出: 单 strategy_search_request 含合并 evidence 提示
"systemic" + triggered_by="anomaly_cluster" (与单个 anomaly_threshold 区分).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.anomaly_cluster")


DEFAULT_CLUSTER_MIN_KINDS = 2
"""同 target_module 至少 N 个不同 anomaly_kind → cluster."""

DEFAULT_CLUSTER_MIN_MODULES_FOR_CROSS = 2
"""同 anomaly_kind 至少 N 个不同 target_module → cross-module cluster."""

DEFAULT_TENANT_DEGRADATION_THRESHOLD = 3
"""同 tenant 至少 N 个独立 anomaly → tenant_degradation cluster."""


@dataclass(frozen=True)
class AnomalyCluster:
    """聚类结果."""

    cluster_id: str
    cluster_kind: str  # module_systemic / cross_module_pattern / tenant_degradation
    tenant_id: str
    primary_target_module: str
    anomaly_kinds: list[str]
    target_modules: list[str]
    member_request_ids: list[str]
    combined_evidence: list[dict[str, Any]]
    priority: str  # high (≥ 2 members 自动 high)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _module_prefix(module: str) -> str:
    """取 module 的顶层前缀用于关联性判断: 'executor.coding.refactor' → 'executor'."""
    return module.split(".", 1)[0].split("/", 1)[0]


def cluster_anomalies(
    triggered_requests: list[dict[str, Any]],
    *,
    min_kinds_for_module: int = DEFAULT_CLUSTER_MIN_KINDS,
    min_modules_for_cross: int = DEFAULT_CLUSTER_MIN_MODULES_FOR_CROSS,
    tenant_degradation_threshold: int = DEFAULT_TENANT_DEGRADATION_THRESHOLD,
) -> list[AnomalyCluster]:
    """从一批 triggered strategy_search_requests 聚类.

    Returns: 0 个或多个 AnomalyCluster.

    单 request 不聚类; 至少 ≥ min_kinds_for_module 或对应阈值才形成 cluster.
    """
    if len(triggered_requests) < 2:
        return []

    clusters: list[AnomalyCluster] = []

    # ---- Rule 1: 同 target_module + 多 anomaly_kind ----
    by_module: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for req in triggered_requests:
        key = (req.get("tenant_id", "default"), req.get("target_module", "unknown"))
        by_module[key].append(req)

    seen_member_ids: set[str] = set()
    for (tenant_id, module), members in by_module.items():
        kinds = {m.get("anomaly_kind") for m in members if m.get("anomaly_kind")}
        if len(kinds) >= min_kinds_for_module:
            cluster = _make_cluster(
                cluster_kind="module_systemic",
                tenant_id=tenant_id,
                primary_target_module=module,
                members=members,
            )
            clusters.append(cluster)
            seen_member_ids.update(cluster.member_request_ids)

    # ---- Rule 2: 同 anomaly_kind + 多 target_module 前缀相同 ----
    by_kind_prefix: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for req in triggered_requests:
        if req.get("request_id") in seen_member_ids:
            continue
        kind = req.get("anomaly_kind", "")
        tenant_id = req.get("tenant_id", "default")
        prefix = _module_prefix(req.get("target_module", "unknown"))
        by_kind_prefix[(tenant_id, kind, prefix)].append(req)

    for (tenant_id, _kind, prefix), members in by_kind_prefix.items():
        if len(members) < min_modules_for_cross:
            continue
        modules = {m.get("target_module") for m in members}
        if len(modules) < min_modules_for_cross:
            continue
        cluster = _make_cluster(
            cluster_kind="cross_module_pattern",
            tenant_id=tenant_id,
            primary_target_module=f"{prefix}/<cluster>",
            members=members,
        )
        clusters.append(cluster)
        seen_member_ids.update(cluster.member_request_ids)

    # ---- Rule 3: 同 tenant + ≥ N 个独立 anomaly ----
    by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for req in triggered_requests:
        if req.get("request_id") in seen_member_ids:
            continue
        by_tenant[req.get("tenant_id", "default")].append(req)

    for tenant_id, members in by_tenant.items():
        if len(members) < tenant_degradation_threshold:
            continue
        cluster = _make_cluster(
            cluster_kind="tenant_degradation",
            tenant_id=tenant_id,
            primary_target_module="<tenant-wide>",
            members=members,
        )
        clusters.append(cluster)
        seen_member_ids.update(cluster.member_request_ids)

    return clusters


def _make_cluster(
    *,
    cluster_kind: str,
    tenant_id: str,
    primary_target_module: str,
    members: list[dict[str, Any]],
) -> AnomalyCluster:
    kinds = sorted({m.get("anomaly_kind", "unknown") for m in members})
    modules = sorted({m.get("target_module", "unknown") for m in members})
    member_ids = [m.get("request_id", "") for m in members if m.get("request_id")]
    combined_evidence: list[dict[str, Any]] = []
    for m in members:
        for ev in m.get("evidence") or []:
            combined_evidence.append(
                {
                    **ev,
                    "_from_request_id": m.get("request_id"),
                    "_from_anomaly_kind": m.get("anomaly_kind"),
                }
            )
    # priority: cluster 自动 high (≥ 2 个 member 已超过单异常基线)
    return AnomalyCluster(
        cluster_id=new_id("strategy_search"),
        cluster_kind=cluster_kind,
        tenant_id=tenant_id,
        primary_target_module=primary_target_module,
        anomaly_kinds=kinds,
        target_modules=modules,
        member_request_ids=member_ids,
        combined_evidence=combined_evidence,
        priority="high",
    )


def cluster_to_search_request(cluster: AnomalyCluster) -> dict[str, Any]:
    """AnomalyCluster → strategy_search_request payload 给 Strategist 消费.

    与 _build_request 输出的单 anomaly request 兼容字段; 增加 cluster-specific
    字段让 Strategist 知道这是"系统性问题".
    """
    return {
        "request_id": cluster.cluster_id,
        "tenant_id": cluster.tenant_id,
        "triggered_by": "anomaly_cluster",  # 与单 anomaly 区分
        "target_module": cluster.primary_target_module,
        "anomaly_kind": cluster.cluster_kind,  # cluster_kind 当 anomaly_kind 用
        "evidence": cluster.combined_evidence,
        "priority": cluster.priority,
        "dedup_key": f"{cluster.tenant_id}:cluster:{cluster.cluster_kind}:{cluster.primary_target_module}",
        "status": "open",
        "created_at": cluster.created_at,
        "severity": "strong",
        "escalation_path": ["role", "task", "gate"],
        "is_self_referential": False,
        "repeat_count": len(cluster.member_request_ids),
        "cluster_member_request_ids": cluster.member_request_ids,
        "cluster_anomaly_kinds": cluster.anomaly_kinds,
        "cluster_target_modules": cluster.target_modules,
    }


__all__ = [
    "DEFAULT_CLUSTER_MIN_KINDS",
    "DEFAULT_CLUSTER_MIN_MODULES_FOR_CROSS",
    "DEFAULT_TENANT_DEGRADATION_THRESHOLD",
    "AnomalyCluster",
    "cluster_anomalies",
    "cluster_to_search_request",
]
