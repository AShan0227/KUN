"""Composite tenant PKs for idempotency_keys and experiments.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-25

Background — pre-merge audit found two PKs were single-column and tenant-blind:

  - ``idempotency_keys.key`` (PK) is a prompt fingerprint hash. Two tenants
    asking the exact same prompt produce identical fingerprints. The first
    INSERT wins; the second IntegrityErrors and the orchestrator's recovery
    path can't disambiguate (its query filters on tenant_id, the row owned
    by the other tenant is invisible) → caller hangs.

  - ``experiments.id`` (PK) is user-supplied. Two tenants picking the same
    experiment id collide; one cannot create it. Not a leak, but a forced
    global namespace.

Switching both to composite ``(tenant_id, <key>)`` PKs gives each tenant its
own slot in the keyspace.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # idempotency_keys: (key) → (tenant_id, key)
    op.execute("ALTER TABLE idempotency_keys DROP CONSTRAINT pk_idempotency_keys")
    op.execute("ALTER TABLE idempotency_keys ADD PRIMARY KEY (tenant_id, key)")

    # experiments: (id) → (tenant_id, id)
    op.execute("ALTER TABLE experiments DROP CONSTRAINT pk_experiments")
    op.execute("ALTER TABLE experiments ADD PRIMARY KEY (tenant_id, id)")


def downgrade() -> None:
    # Reversal is data-dependent — if multiple tenants share a key, a single-
    # column PK can't hold them. We pick the most-recently-updated row per
    # key on the way down. Run with caution; intended for emergency rollback
    # only on near-empty tables.
    op.execute(
        """
        DELETE FROM idempotency_keys a USING idempotency_keys b
        WHERE a.key = b.key AND a.tenant_id < b.tenant_id
        """
    )
    op.execute("ALTER TABLE idempotency_keys DROP CONSTRAINT pk_idempotency_keys")
    op.execute("ALTER TABLE idempotency_keys ADD PRIMARY KEY (key)")

    op.execute(
        """
        DELETE FROM experiments a USING experiments b
        WHERE a.id = b.id AND a.tenant_id < b.tenant_id
        """
    )
    op.execute("ALTER TABLE experiments DROP CONSTRAINT pk_experiments")
    op.execute("ALTER TABLE experiments ADD PRIMARY KEY (id)")
