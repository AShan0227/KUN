"""Distill repeated NUO context-governance findings into review-only rules.

Context maintenance can mark many individual assets as duplicate, low-value,
stale, or compiler-limited.  This module turns repeated patterns into small
methodology drafts so KUN learns *why* future context should be compressed,
down-ranked, recompiled, or forgotten.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import AssetKind, AssetLayer, LayeredAsset
from kun.context.storage import AssetStore, get_store

GovernanceRuleCategory = Literal[
    "duplicate_pattern",
    "low_value_pattern",
    "stale_or_risky_pattern",
    "compiler_quality_pattern",
]


class ContextGovernanceRuleDraft(BaseModel):
    """Review-only rule draft distilled from repeated context governance facts."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    category: GovernanceRuleCategory
    asset_kind: AssetKind
    trigger: str
    recommendation: str
    evidence_count: int
    evidence_asset_ids: list[str] = Field(default_factory=list)
    production_action: Literal[False] = False
    requires_human_review: bool = True


class ContextGovernanceRuleDistillReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    scanned: int = 0
    candidates: int = 0
    created: int = 0
    updated: int = 0
    dry_run: bool = True
    min_evidence: int = 2
    drafts: list[ContextGovernanceRuleDraft] = Field(default_factory=list)
    production_action: Literal[False] = False


async def distill_context_governance_rules(
    *,
    tenant_id: str,
    store: AssetStore | None = None,
    dry_run: bool = True,
    min_evidence: int = 2,
    max_assets: int = 1000,
) -> ContextGovernanceRuleDistillReport:
    """Create methodology drafts from repeated NUO context governance patterns."""

    store = store or get_store()
    assets = await _list_context_assets(store, tenant_id=tenant_id, limit=max_assets)
    groups: dict[tuple[GovernanceRuleCategory, AssetKind, str], list[LayeredAsset]] = defaultdict(
        list
    )
    for asset in assets:
        for category, trigger in _asset_governance_patterns(asset):
            groups[(category, asset.asset_kind, trigger)].append(asset)

    drafts = [
        _draft_from_group(category, asset_kind, trigger, group)
        for (category, asset_kind, trigger), group in groups.items()
        if len(group) >= max(1, min_evidence)
    ]
    drafts.sort(key=lambda item: (-item.evidence_count, item.category, item.asset_kind))

    created = 0
    updated = 0
    if not dry_run and drafts:
        existing = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=max_assets)
        existing_by_rule = {
            str(asset.l1_metadata.get("rule_id")): asset
            for asset in existing
            if asset.l1_metadata.get("source") == "context.governance_rule_distill"
            and asset.l1_metadata.get("rule_id")
        }
        for draft in drafts:
            existing_asset = existing_by_rule.get(draft.rule_id)
            if existing_asset is None:
                await store.put(_asset_from_draft(tenant_id=tenant_id, draft=draft))
                created += 1
            else:
                changed = _merge_rule_evidence(existing_asset, draft)
                if changed:
                    await store.put(existing_asset)
                    updated += 1

    return ContextGovernanceRuleDistillReport(
        tenant_id=tenant_id,
        scanned=len(assets),
        candidates=len(drafts),
        created=created,
        updated=updated,
        dry_run=dry_run,
        min_evidence=max(1, min_evidence),
        drafts=drafts,
    )


async def _list_context_assets(
    store: AssetStore,
    *,
    tenant_id: str,
    limit: int,
) -> list[LayeredAsset]:
    assets: list[LayeredAsset] = []
    for kind in _ASSET_KINDS:
        assets.extend(await store.list(tenant_id=tenant_id, asset_kind=kind, limit=limit))
    return assets[:limit]


def _asset_governance_patterns(
    asset: LayeredAsset,
) -> list[tuple[GovernanceRuleCategory, str]]:
    metadata = asset.l1_metadata or {}
    tags = {str(tag).lower() for tag in asset.tags}
    patterns: list[tuple[GovernanceRuleCategory, str]] = []
    source = _source_bucket(metadata)
    if (
        _truthy(metadata.get("duplicate_candidate"))
        or _truthy(metadata.get("duplicate_merge_applied"))
        or "duplicate_merged" in tags
    ):
        patterns.append(("duplicate_pattern", f"{asset.asset_kind}:duplicate:{source}"))
    if _truthy(metadata.get("low_value")) or "low_value" in tags:
        patterns.append(("low_value_pattern", f"{asset.asset_kind}:low_value:{source}"))
    if _truthy(metadata.get("stale_or_risky")) or "stale_or_risky" in tags:
        risk = str(metadata.get("risk_level") or metadata.get("risk") or "unknown")
        patterns.append(("stale_or_risky_pattern", f"{asset.asset_kind}:stale_risk:{risk}"))
    if _truthy(metadata.get("compiler_recompile_recommended")) or _truthy(
        metadata.get("compiler_review_required")
    ):
        profile = _compiler_profile_bucket(metadata)
        patterns.append(("compiler_quality_pattern", f"{asset.asset_kind}:compiler:{profile}"))
    return patterns


def _draft_from_group(
    category: GovernanceRuleCategory,
    asset_kind: AssetKind,
    trigger: str,
    assets: list[LayeredAsset],
) -> ContextGovernanceRuleDraft:
    evidence_ids = [asset.asset_id for asset in assets[:12]]
    rule_id = _rule_id(category, asset_kind, trigger)
    return ContextGovernanceRuleDraft(
        rule_id=rule_id,
        category=category,
        asset_kind=asset_kind,
        trigger=trigger,
        recommendation=_recommendation(category, asset_kind, trigger),
        evidence_count=len(assets),
        evidence_asset_ids=evidence_ids,
    )


def _asset_from_draft(*, tenant_id: str, draft: ContextGovernanceRuleDraft) -> LayeredAsset:
    return LayeredAsset.build(
        "methodology",
        tenant_id,
        metadata={
            "source": "context.governance_rule_distill",
            "memory_layer": "methodology",
            "rule_id": draft.rule_id,
            "category": draft.category,
            "asset_kind_scope": draft.asset_kind,
            "trigger": draft.trigger,
            "recommendation": draft.recommendation,
            "evidence_count": draft.evidence_count,
            "evidence_asset_ids": draft.evidence_asset_ids,
            "requires_human_review": True,
            "production_action": False,
            "promotion_allowed": False,
            "status": "draft",
        },
        summary=(
            f"Review-only context governance rule {draft.rule_id}: "
            f"when {draft.trigger}, {draft.recommendation}. "
            f"Evidence assets={draft.evidence_count}; production_action=false."
        ),
        layer=AssetLayer.L2_PROJECT,
        tags=[
            "context_governance",
            "methodology",
            "review_only",
            "governance_rule_draft",
            f"category:{draft.category}",
        ],
    )


def _merge_rule_evidence(asset: LayeredAsset, draft: ContextGovernanceRuleDraft) -> bool:
    current_ids = [str(item) for item in asset.l1_metadata.get("evidence_asset_ids") or []]
    merged_ids = list(dict.fromkeys([*current_ids, *draft.evidence_asset_ids]))
    changed = False
    if merged_ids != current_ids:
        asset.l1_metadata["evidence_asset_ids"] = merged_ids[:24]
        changed = True
    if int(asset.l1_metadata.get("evidence_count") or 0) != draft.evidence_count:
        asset.l1_metadata["evidence_count"] = draft.evidence_count
        changed = True
    return changed


def _recommendation(
    category: GovernanceRuleCategory,
    asset_kind: AssetKind,
    trigger: str,
) -> str:
    if category == "duplicate_pattern":
        return (
            f"同类 {asset_kind} 多次重复时，优先合并到主资产并软遗忘重复项，"
            "不要把重复材料都塞进热上下文"
        )
    if category == "low_value_pattern":
        return f"同类 {asset_kind} 多次低价值时，默认降权、限制召回数量，只有任务强相关时再按需加载"
    if category == "stale_or_risky_pattern":
        return f"命中 {trigger} 的资产先走傩复查；高风险或过期内容不要直接进入执行上下文"
    return (
        "编译质量不足的资料先重新编译或换后端，再进入长期记忆；"
        "不要把 placeholder/OCR 缺失内容当成可靠知识"
    )


def _source_bucket(metadata: dict[str, Any]) -> str:
    source = str(metadata.get("source") or metadata.get("compiler") or "unknown").lower()
    if "compiler" in source:
        return "compiler"
    if "qi" in source:
        return "qi"
    if "task" in source:
        return "task"
    return source[:40] or "unknown"


def _compiler_profile_bucket(metadata: dict[str, Any]) -> str:
    profile = metadata.get("compiler_profile")
    if isinstance(profile, dict):
        name = str(profile.get("name") or profile.get("backend") or "").lower()
        if name:
            return name[:40]
        limitations = profile.get("limitations")
        if isinstance(limitations, list) and limitations:
            return "limited"
    compiler = str(metadata.get("compiler") or "").lower()
    return compiler[:40] or "unknown"


def _rule_id(category: GovernanceRuleCategory, asset_kind: AssetKind, trigger: str) -> str:
    digest = hashlib.sha256(f"{category}:{asset_kind}:{trigger}".encode()).hexdigest()
    return f"cgr_{digest[:16]}"


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


__all__ = [
    "ContextGovernanceRuleDistillReport",
    "ContextGovernanceRuleDraft",
    "distill_context_governance_rules",
]
