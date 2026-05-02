"""LayeredAsset (ADR-018 §16.7) — 统一三级渐进披露的存取接口.

合并前: Skill / 记忆 / 知识 / 通讯 / TASK.md / 角色模板各自实现三级存取.
合并后: 所有资产通过 LayeredAsset 基类访问 L1/L2/L3.

收益: 前缀缓存策略一处实现对所有资产生效; 新资产类型零额外成本接入.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import EntityKind, new_id

AssetKind = Literal[
    "skill",
    "memory",
    "knowledge",
    "task",
    "handoff",
    "role_template",
    "methodology",
]


class AssetLayer(StrEnum):
    """Asset reuse scope.

    L1 is single-task only. Higher layers can be reused more broadly and
    therefore need stricter promotion rules.
    """

    L1_TASK = "L1_task"
    L2_PROJECT = "L2_project"
    L3_USER = "L3_user"
    L4_GLOBAL = "L4_global"


# Map AssetKind → id prefix (must exist in kun.core.ids._PREFIX)
_KIND_TO_ID: dict[AssetKind, EntityKind] = {
    "skill": "skill",
    "memory": "memory",
    "knowledge": "memory",  # knowledge items use memory prefix
    "task": "task",
    "handoff": "handoff",
    "role_template": "role_tpl",
    "methodology": "memory",
}


class LayeredAsset(BaseModel):
    """Base class for any asset that supports 3-layer progressive disclosure.

    Layer 1 (metadata): always in context. Small dict.
    Layer 2 (summary/interface): loaded when relevant.
    Layer 3 (full content): loaded only when needed; stored by reference.

    Storage:
        L1 in Postgres JSONB (fast query, small)
        L2 in Postgres JSONB (medium size)
        L3 in object store (s3://) or large Postgres rows
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(
        default_factory=lambda: new_id("memory")
    )  # placeholder, override per kind
    asset_kind: AssetKind
    tenant_id: str
    # L1 — always loaded
    l1_metadata: dict[str, Any] = Field(default_factory=dict)
    # L2 — loaded on demand (summary / interface spec)
    l2_summary: str | None = None
    # L3 — only when needed
    l3_ref: str | None = None  # s3://... or internal ref
    # Reuse scope
    layer: AssetLayer = AssetLayer.L1_TASK
    # Meta
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_accessed: datetime = Field(default_factory=lambda: datetime.now(UTC))
    access_count: int = 0
    version: int = 1

    def touch(self) -> None:
        """Register an access — 强化重要度."""
        self.last_accessed = datetime.now(UTC)
        self.access_count += 1

    def clone_for_layer(self, layer: AssetLayer) -> LayeredAsset:
        """Return a copy promoted to a broader layer."""
        return self.model_copy(
            deep=True,
            update={
                "layer": layer,
                "version": self.version + 1,
            },
        )

    def anonymized_for_global(self) -> LayeredAsset:
        """Return an L4-safe copy with obvious user identifiers stripped."""
        safe = self.clone_for_layer(AssetLayer.L4_GLOBAL)
        safe.tenant_id = "global"
        safe.l1_metadata = _anonymize_metadata(safe.l1_metadata)
        if safe.l2_summary is not None:
            safe.l2_summary = anonymize_text(safe.l2_summary)
        safe.tags = [anonymize_text(tag) for tag in safe.tags if not _looks_sensitive_key(tag)]
        safe.l1_metadata["anonymized"] = True
        safe.l1_metadata["source_asset_id"] = self.asset_id
        return safe

    @classmethod
    def build(
        cls,
        asset_kind: AssetKind,
        tenant_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        summary: str | None = None,
        full_ref: str | None = None,
        layer: AssetLayer = AssetLayer.L1_TASK,
        tags: list[str] | None = None,
    ) -> LayeredAsset:
        """Factory with prefix-appropriate id."""
        prefix_kind = _KIND_TO_ID[asset_kind]
        return cls(
            asset_id=new_id(prefix_kind),
            asset_kind=asset_kind,
            tenant_id=tenant_id,
            l1_metadata=metadata or {},
            l2_summary=summary,
            l3_ref=full_ref,
            layer=layer,
            tags=tags or [],
        )


_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_SENSITIVE_KEYS = {
    "email",
    "phone",
    "mobile",
    "name",
    "full_name",
    "user",
    "user_id",
    "tenant",
    "tenant_id",
    "customer",
    "customer_id",
    "client",
    "account",
    "address",
}


def anonymize_text(text: str) -> str:
    """Remove common direct identifiers from reusable global text."""
    text = _EMAIL_RE.sub("[email]", text)
    return _PHONE_RE.sub("[phone]", text)


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEYS)


def _anonymize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if _looks_sensitive_key(key):
            continue
        if isinstance(value, str):
            safe[key] = anonymize_text(value)
        elif isinstance(value, list):
            safe[key] = [anonymize_text(item) if isinstance(item, str) else item for item in value]
        elif isinstance(value, dict):
            safe[key] = _anonymize_metadata(value)
        else:
            safe[key] = value
    return safe
