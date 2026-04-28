"""V2.3 Wire 43: pheromone — entity_relationships 加 pheromone_strength.

Revision ID: 0016_pheromone
Revises: 0015_protocols
Create Date: 2026-04-27

V2.3 §6 生物群体 swarm 启发: 不是"显式记规则", 是让规则从行为里自然涌现.
- 每次 task 走过路径 → pheromone +0.05
- 每日衰减: pheromone × 0.95 (没人走的慢慢遗忘)
- GraphTraversal 选邻居时按 confidence × (0.5 + pheromone) (0.5 基础, 加成)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_pheromone"
down_revision: str | None = "0015_protocols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entity_relationships",
        sa.Column(
            "pheromone_strength",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
    )
    op.create_check_constraint(
        "entity_relationship_pheromone_range",
        "entity_relationships",
        "pheromone_strength >= 0 AND pheromone_strength <= 1",
    )


def downgrade() -> None:
    op.drop_constraint("entity_relationship_pheromone_range", "entity_relationships")
    op.drop_column("entity_relationships", "pheromone_strength")
