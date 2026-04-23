"""KUN core shared abstractions (ADR-018 §16 consolidations)."""

from kun.core.ids import new_id
from kun.core.scoring import ScoreDescriptor, ScoreKind
from kun.core.tenancy import TenantContext, current_tenant

__all__ = [
    "ScoreDescriptor",
    "ScoreKind",
    "TenantContext",
    "current_tenant",
    "new_id",
]
