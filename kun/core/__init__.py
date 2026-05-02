"""KUN core shared abstractions (ADR-018 §16 consolidations)."""

from kun.core.ids import new_id
from kun.core.scoring import ScoreDescriptor, ScoreKind
from kun.core.state_ledger import StateLedger, StateLedgerEntry, get_state_ledger
from kun.core.tenancy import TenantContext, current_tenant

__all__ = [
    "ScoreDescriptor",
    "ScoreKind",
    "StateLedger",
    "StateLedgerEntry",
    "TenantContext",
    "current_tenant",
    "get_state_ledger",
    "new_id",
]
