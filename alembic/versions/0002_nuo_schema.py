"""傩 (NUO) 独立 schema (ADR-012).

创建 nuo schema, 未来傩的表都放在里面. 现在只是建空 schema 占位.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS nuo")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS nuo CASCADE")
