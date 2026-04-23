"""LayeredAsset (ADR-018 §16.7) — 统一三级渐进披露的存取接口.

合并前: Skill / 记忆 / 知识 / 通讯 / TASK.md / 角色模板各自实现三级存取.
合并后: 所有资产通过 LayeredAsset 基类访问 L1/L2/L3.

收益: 前缀缓存策略一处实现对所有资产生效; 新资产类型零额外成本接入.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

    @classmethod
    def build(
        cls,
        asset_kind: AssetKind,
        tenant_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        summary: str | None = None,
        full_ref: str | None = None,
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
            tags=tags or [],
        )
