"""Review-only memory/context governance audit.

This is the read-only side of NUO memory slimming.  It inspects context assets
and produces recommendations, but never writes, compresses, forgets, merges, or
deletes production assets.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import AssetKind, AssetLayer, LayeredAsset
from kun.context.storage import AssetStore, get_store

GovernanceAuditCategory = Literal[
    "low_value",
    "duplicate",
    "high_frequency_abstractable",
    "stale_long_tail",
    "missing_credit_attribution",
]
GovernanceAuditSeverity = Literal["info", "warn"]


class ContextGovernanceAuditFinding(BaseModel):
    """One review-only governance recommendation for memory/context assets."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    category: GovernanceAuditCategory
    severity: GovernanceAuditSeverity
    asset_kind: AssetKind
    asset_ids: list[str]
    title: str
    reason: str
    recommendation: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    review_only: Literal[True] = True
    production_action: Literal[False] = False


class ContextGovernanceAuditReport(BaseModel):
    """Read-only NUO audit report for memory/context slimming."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    total_seen: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)
    findings: list[ContextGovernanceAuditFinding] = Field(default_factory=list)
    review_only: Literal[True] = True
    production_action: Literal[False] = False


async def run_context_governance_audit(
    *,
    tenant_id: str,
    store: AssetStore | None = None,
    max_assets: int = 1000,
    low_value_after_days: int = 14,
    stale_after_days: int = 60,
    long_tail_max_access_count: int = 1,
    duplicate_min_summary_chars: int = 20,
    high_frequency_min_assets: int = 2,
    high_frequency_total_access: int = 6,
    credited_resource_keys: Iterable[str] | None = None,
) -> ContextGovernanceAuditReport:
    """Inspect assets and return review-only slimming recommendations."""

    store = store or get_store()
    assets = await _list_context_assets(store, tenant_id=tenant_id, limit=max_assets)
    now = datetime.now(UTC)
    credited = {str(key) for key in credited_resource_keys or [] if str(key)}
    findings: list[ContextGovernanceAuditFinding] = []

    findings.extend(
        _duplicate_findings(
            assets,
            duplicate_min_summary_chars=duplicate_min_summary_chars,
        )
    )
    findings.extend(
        _low_value_and_stale_findings(
            assets,
            now=now,
            low_value_after_days=low_value_after_days,
            stale_after_days=stale_after_days,
            long_tail_max_access_count=long_tail_max_access_count,
        )
    )
    findings.extend(
        _high_frequency_findings(
            assets,
            high_frequency_min_assets=high_frequency_min_assets,
            high_frequency_total_access=high_frequency_total_access,
        )
    )
    findings.extend(_missing_credit_findings(assets, credited_resource_keys=credited))

    findings = _dedupe_findings(findings)
    findings.sort(key=_finding_sort_key)
    counts = Counter(finding.category for finding in findings)
    return ContextGovernanceAuditReport(
        tenant_id=tenant_id,
        total_seen=len(assets),
        category_counts={category: int(count) for category, count in sorted(counts.items())},
        findings=findings,
    )


async def _list_context_assets(
    store: AssetStore,
    *,
    tenant_id: str,
    limit: int,
) -> list[LayeredAsset]:
    assets: list[LayeredAsset] = []
    per_kind_limit = max(1, limit)
    for kind in _ASSET_KINDS:
        assets.extend(await store.list(tenant_id=tenant_id, asset_kind=kind, limit=per_kind_limit))
        if len(assets) >= limit:
            return assets[:limit]
    return assets[:limit]


def _duplicate_findings(
    assets: list[LayeredAsset],
    *,
    duplicate_min_summary_chars: int,
) -> list[ContextGovernanceAuditFinding]:
    groups: dict[tuple[AssetKind, str], list[LayeredAsset]] = defaultdict(list)
    for asset in assets:
        normalized = _normalize_text(asset.l2_summary or "")
        if len(normalized) >= duplicate_min_summary_chars:
            groups[(asset.asset_kind, normalized)].append(asset)

    findings: list[ContextGovernanceAuditFinding] = []
    for (asset_kind, normalized), group in groups.items():
        if len(group) < 2:
            continue
        asset_ids = [asset.asset_id for asset in group]
        findings.append(
            _finding(
                category="duplicate",
                severity="info",
                asset_kind=asset_kind,
                asset_ids=asset_ids,
                title="重复 context / memory 资产候选",
                reason=f"{len(group)} 个同类资产拥有相同 L2 summary。",
                recommendation=("人工确认语义等价后再合并或软遗忘重复项；审计报告本身不改资产。"),
                evidence={
                    "normalized_summary_sha256": _digest(normalized),
                    "duplicate_count": len(group),
                },
            )
        )
    return findings


def _low_value_and_stale_findings(
    assets: list[LayeredAsset],
    *,
    now: datetime,
    low_value_after_days: int,
    stale_after_days: int,
    long_tail_max_access_count: int,
) -> list[ContextGovernanceAuditFinding]:
    findings: list[ContextGovernanceAuditFinding] = []
    for asset in assets:
        if _is_permanent(asset):
            continue
        age_days = max(0.0, (now - asset.last_accessed).total_seconds() / 86400)
        tags = {str(tag).lower() for tag in asset.tags}
        metadata = asset.l1_metadata or {}
        if (
            "low_value" in tags
            or _truthy(metadata.get("low_value"))
            or (asset.access_count == 0 and age_days >= low_value_after_days)
        ):
            findings.append(
                _finding(
                    category="low_value",
                    severity="info",
                    asset_kind=asset.asset_kind,
                    asset_ids=[asset.asset_id],
                    title="低价值记忆候选",
                    reason=(
                        f"access_count={asset.access_count}; "
                        f"last_accessed_age_days={age_days:.0f}; "
                        "未见近期复用或已带低价值标签。"
                    ),
                    recommendation=(
                        "先降权或放入人工复核队列；只有确认无引用、无贡献后再考虑维护执行。"
                    ),
                    evidence=_asset_evidence(asset, age_days=age_days),
                )
            )
        if age_days >= stale_after_days and asset.access_count <= long_tail_max_access_count:
            findings.append(
                _finding(
                    category="stale_long_tail",
                    severity="warn",
                    asset_kind=asset.asset_kind,
                    asset_ids=[asset.asset_id],
                    title="过期 / 长尾资产候选",
                    reason=(
                        f"{age_days:.0f} 天未访问，access_count={asset.access_count}，"
                        "仍占用 context 检索面。"
                    ),
                    recommendation=(
                        "人工确认是否仍有审计价值；优先压缩、归档或降权，不从审计报告直接删除。"
                    ),
                    evidence=_asset_evidence(asset, age_days=age_days),
                )
            )
    return findings


def _high_frequency_findings(
    assets: list[LayeredAsset],
    *,
    high_frequency_min_assets: int,
    high_frequency_total_access: int,
) -> list[ContextGovernanceAuditFinding]:
    groups: dict[tuple[AssetKind, str], list[LayeredAsset]] = defaultdict(list)
    for asset in assets:
        key = _abstraction_group_key(asset)
        if key:
            groups[(asset.asset_kind, key)].append(asset)

    findings: list[ContextGovernanceAuditFinding] = []
    for (asset_kind, key), group in groups.items():
        total_access = sum(max(0, asset.access_count) for asset in group)
        if len(group) < high_frequency_min_assets or total_access < high_frequency_total_access:
            continue
        asset_ids = [asset.asset_id for asset in group[:12]]
        findings.append(
            _finding(
                category="high_frequency_abstractable",
                severity="info",
                asset_kind=asset_kind,
                asset_ids=asset_ids,
                title="高频经验可抽象候选",
                reason=(f"{len(group)} 个资产命中同一模式 {key!r}，累计访问 {total_access} 次。"),
                recommendation=(
                    "抽成 review-only methodology 草案或策略规则，再由人/强评审确认是否进入长期层。"
                ),
                evidence={
                    "group_key": key,
                    "group_size": len(group),
                    "total_access_count": total_access,
                },
            )
        )
    return findings


def _missing_credit_findings(
    assets: list[LayeredAsset],
    *,
    credited_resource_keys: set[str],
) -> list[ContextGovernanceAuditFinding]:
    findings: list[ContextGovernanceAuditFinding] = []
    for asset in assets:
        if not _should_expect_credit(asset):
            continue
        if _has_credit_attribution(asset, credited_resource_keys=credited_resource_keys):
            continue
        findings.append(
            _finding(
                category="missing_credit_attribution",
                severity="warn",
                asset_kind=asset.asset_kind,
                asset_ids=[asset.asset_id],
                title="缺少信用归因的可复用资产",
                reason=(
                    "资产已被访问、晋升或处于可复用记忆层，但没有可见 resource_credit "
                    "或 metadata 贡献归因。"
                ),
                recommendation=(
                    "补充任务/结果/贡献来源，让后续瘦身能按真实贡献决策，而不是只看年龄和文本。"
                ),
                evidence={
                    **_asset_evidence(asset),
                    "expected_credit_keys": sorted(_credit_keys_for(asset)),
                },
            )
        )
    return findings


def _finding(
    *,
    category: GovernanceAuditCategory,
    severity: GovernanceAuditSeverity,
    asset_kind: AssetKind,
    asset_ids: list[str],
    title: str,
    reason: str,
    recommendation: str,
    evidence: dict[str, Any],
) -> ContextGovernanceAuditFinding:
    fingerprint = _digest(f"{category}:{asset_kind}:{','.join(sorted(asset_ids))}:{reason}")
    return ContextGovernanceAuditFinding(
        finding_id=f"ctx_audit_{fingerprint[:16]}",
        category=category,
        severity=severity,
        asset_kind=asset_kind,
        asset_ids=asset_ids,
        title=title,
        reason=reason,
        recommendation=recommendation,
        evidence=evidence,
    )


def _dedupe_findings(
    findings: list[ContextGovernanceAuditFinding],
) -> list[ContextGovernanceAuditFinding]:
    seen: set[str] = set()
    out: list[ContextGovernanceAuditFinding] = []
    for finding in findings:
        if finding.finding_id in seen:
            continue
        seen.add(finding.finding_id)
        out.append(finding)
    return out


def _finding_sort_key(finding: ContextGovernanceAuditFinding) -> tuple[int, str, str]:
    severity_rank = {"warn": 0, "info": 1}
    return (severity_rank[finding.severity], finding.category, finding.finding_id)


def _asset_evidence(asset: LayeredAsset, *, age_days: float | None = None) -> dict[str, Any]:
    metadata = asset.l1_metadata or {}
    evidence: dict[str, Any] = {
        "asset_id": asset.asset_id,
        "asset_kind": asset.asset_kind,
        "layer": asset.layer.value,
        "access_count": asset.access_count,
        "memory_layer": metadata.get("memory_layer"),
        "task_type": metadata.get("task_type"),
        "tags": list(asset.tags[:8]),
    }
    if age_days is not None:
        evidence["last_accessed_age_days"] = round(age_days, 2)
    return evidence


def _abstraction_group_key(asset: LayeredAsset) -> str:
    metadata = asset.l1_metadata or {}
    memory_layer = str(metadata.get("memory_layer") or "").strip().lower()
    task_type = str(metadata.get("task_type") or "").strip().lower()
    if memory_layer in {"task_result", "execution_process", "meta_decision"} and task_type:
        strategy = str(metadata.get("strategy_pack_id") or "").strip().lower()
        skill = str(metadata.get("skill_used") or "").strip().lower()
        status = str(metadata.get("status") or "").strip().lower()
        parts = [memory_layer, task_type]
        if strategy:
            parts.append(f"strategy={strategy}")
        if skill:
            parts.append(f"skill={skill}")
        if status:
            parts.append(f"status={status}")
        return "|".join(parts)
    reusable_tags = [
        str(tag).strip().lower()
        for tag in asset.tags
        if str(tag).strip().lower() not in _GENERIC_TAGS
    ]
    if asset.access_count > 0 and reusable_tags:
        return f"tag:{reusable_tags[0]}"
    return ""


def _should_expect_credit(asset: LayeredAsset) -> bool:
    metadata = asset.l1_metadata or {}
    memory_layer = str(metadata.get("memory_layer") or "")
    if asset.asset_kind == "skill":
        return True
    if asset.access_count > 0:
        return True
    if asset.layer in {AssetLayer.L2_PROJECT, AssetLayer.L3_USER, AssetLayer.L4_GLOBAL}:
        return asset.asset_kind in {"memory", "knowledge", "methodology", "skill"}
    return memory_layer in {"task_result", "meta_decision", "execution_process"}


def _has_credit_attribution(
    asset: LayeredAsset,
    *,
    credited_resource_keys: set[str],
) -> bool:
    if _credit_keys_for(asset) & credited_resource_keys:
        return True
    metadata = asset.l1_metadata or {}
    if any(
        key in metadata
        for key in (
            "resource_credit",
            "credit_total",
            "contribution_score",
            "credit_assignment",
            "credit_source",
        )
    ):
        return True
    decision_tickets = metadata.get("decision_tickets")
    return isinstance(decision_tickets, list) and bool(decision_tickets)


def _credit_keys_for(asset: LayeredAsset) -> set[str]:
    keys = {
        asset.asset_id,
        f"{asset.asset_kind}:{asset.asset_id}",
    }
    if asset.asset_kind in {"memory", "knowledge", "methodology"}:
        keys.add(f"memory:{asset.asset_id}")
    return keys


def _is_permanent(asset: LayeredAsset) -> bool:
    return str((asset.l1_metadata or {}).get("tier") or "").lower() == "permanent"


def _normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"true", "1", "yes"}


_ASSET_KINDS: tuple[AssetKind, ...] = (
    "memory",
    "knowledge",
    "methodology",
    "skill",
    "role_template",
    "task",
    "handoff",
)
_GENERIC_TAGS = {
    "v3",
    "memory",
    "knowledge",
    "methodology",
    "review_only",
    "context_governance",
}
_WHITESPACE_RE = re.compile(r"\s+")


__all__ = [
    "ContextGovernanceAuditFinding",
    "ContextGovernanceAuditReport",
    "run_context_governance_audit",
]
