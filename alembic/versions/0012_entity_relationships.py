"""Add entity_relationships knowledge graph table.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "entity_relationships",
        sa.Column("relation_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_entity_kind", sa.String(length=64), nullable=False),
        sa.Column("source_entity_id", sa.String(length=128), nullable=False),
        sa.Column("target_entity_kind", sa.String(length=64), nullable=False),
        sa.Column("target_entity_id", sa.String(length=128), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.3"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_reinforced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="entity_relationship_confidence_range",
        ),
        sa.CheckConstraint(
            "evidence_count >= 0",
            name="entity_relationship_evidence_nonnegative",
        ),
        sa.CheckConstraint(
            "relation_type IN ("
            "'depends_on','mentions','verifies','contradicts','similar_to',"
            "'co_occurs','produced_by','transfer_confidence'"
            ")",
            name="entity_relationship_type_valid",
        ),
        sa.PrimaryKeyConstraint("relation_id", "tenant_id", name="pk_entity_relationships"),
    )
    op.create_index(
        "ix_relationships_tenant_source",
        "entity_relationships",
        ["tenant_id", "source_entity_kind", "source_entity_id"],
    )
    op.create_index(
        "ix_relationships_tenant_target",
        "entity_relationships",
        ["tenant_id", "target_entity_kind", "target_entity_id"],
    )
    op.execute("ALTER TABLE entity_relationships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entity_relationships FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON entity_relationships
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON entity_relationships")
    op.execute("ALTER TABLE entity_relationships NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entity_relationships DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_relationships_tenant_target", table_name="entity_relationships")
    op.drop_index("ix_relationships_tenant_source", table_name="entity_relationships")
    op.drop_table("entity_relationships")
