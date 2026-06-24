"""Align capability_cards.entity_type CHECK with the EntityType enum (audit F054).

The DB CHECK (migration 0008) allowed
  role_template, model, skill, tool, human, external_agent
while the Python EntityType enum allowed
  role_template, human, external_agent, company, model
so a card with entity_type='company' passed Pydantic but was rejected by the DB,
and 'skill'/'tool' rows passed the DB but failed Pydantic on read. This widens
the CHECK to the union (adds 'company') so both layers agree.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_capability_cards_capability_entity_type_valid"
_TABLE = "capability_cards"

_UNION = (
    "entity_type IN ('role_template', 'model', 'human', 'external_agent', "
    "'company', 'skill', 'tool')"
)
_OLD = "entity_type IN ('role_template', 'model', 'skill', 'tool', 'human', 'external_agent')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _UNION)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _OLD)
