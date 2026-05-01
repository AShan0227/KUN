"""Canonical compiler models for lightweight KUN materials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CanonicalKind = Literal["text", "markdown", "html", "json", "csv", "pdf", "unsupported"]
CompileStatus = Literal["compiled", "placeholder", "rejected", "unsupported"]


class MaterialSource(BaseModel):
    """Where a material came from and how much the compiler actually touched it."""

    type: Literal["inline", "path", "url", "bytes"]
    uri: str
    detected_kind: str | None = None
    mime_type: str | None = None


class MaterialRisk(BaseModel):
    """Conservative risk labels surfaced with every compiler result."""

    level: Literal["low", "medium", "high"] = "low"
    flags: list[str] = Field(default_factory=list)
    reason: str = ""


class MaterialPermissions(BaseModel):
    """Current permission stance for downstream storage and reuse."""

    read: bool = True
    transform: bool = True
    store_l1: bool = True
    store_l2: bool = True
    store_l3: bool = False
    notes: list[str] = Field(default_factory=list)


class MaterialProvenance(BaseModel):
    """Audit information for a compiler run."""

    compiler: str = "kun.compiler"
    compiled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    input_sha256: str | None = None
    detector: str | None = None
    backend: str = "deterministic-lightweight"
    notes: list[str] = Field(default_factory=list)


class CompilerProfile(BaseModel):
    """Honest capabilities and limits of the selected compiler path."""

    name: str = "kun-v5-lightweight"
    version: str = "0.1"
    optional_backends: list[str] = Field(default_factory=list)
    unsupported_backends: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CanonicalMaterial(BaseModel):
    """A lightweight, canonical representation ready for later KUN layers."""

    asset_id: str
    kind: CanonicalKind
    source: MaterialSource
    tenant_id: str
    l1: str
    l2: str
    l3_ref: str | None = None
    tokens_estimate: int
    risk: MaterialRisk
    permissions: MaterialPermissions
    provenance: MaterialProvenance
    compiler_profile: CompilerProfile
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: CompileStatus = "compiled"

    @field_validator("tokens_estimate", mode="before")
    @classmethod
    def _non_negative_tokens(cls, value: Any) -> int:
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            tokens = 0
        return max(0, tokens)


CanonicalAsset = CanonicalMaterial


__all__ = [
    "CanonicalAsset",
    "CanonicalKind",
    "CanonicalMaterial",
    "CompileStatus",
    "CompilerProfile",
    "MaterialPermissions",
    "MaterialProvenance",
    "MaterialRisk",
    "MaterialSource",
]
