"""bug_root_cause_cases unique-signature is a unique INDEX, matching migration 0013 (audit F157).

The ORM previously declared a UniqueConstraint named ix_bug_cases_signature while
migration 0013 created a unique *index* of that name — different catalog objects,
so `alembic check` would flag a drift. Guard that the ORM matches the migration.
"""

from __future__ import annotations

import pytest
from kun.core.orm import BugRootCaseRow
from sqlalchemy import UniqueConstraint


@pytest.mark.unit
def test_signature_is_unique_index() -> None:
    table = BugRootCaseRow.__table__
    by_name = {idx.name: idx for idx in table.indexes}
    assert "ix_bug_cases_signature" in by_name, "unique index missing from ORM"
    sig = by_name["ix_bug_cases_signature"]
    assert sig.unique is True
    assert [c.name for c in sig.columns] == ["tenant_id", "trace_signature"]


@pytest.mark.unit
def test_no_unique_constraint_of_that_name() -> None:
    # A UniqueConstraint named ix_bug_cases_signature would diverge from the DB
    # (migration 0013 uses a unique index), re-introducing the F157 drift.
    table = BugRootCaseRow.__table__
    uc_names = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert "ix_bug_cases_signature" not in uc_names
