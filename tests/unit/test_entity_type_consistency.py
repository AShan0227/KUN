"""EntityType enum ↔ DB CHECK consistency (audit F054).

The Python EntityType enum, the ORM CheckConstraint, and migration 0019 must all
allow the same set of entity_type values. They had drifted (enum had 'company'
the CHECK rejected; CHECK had 'skill'/'tool' the enum rejected). This guards
against re-drift.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest
from kun.core.orm import CapabilityCardRow
from kun.datamodel.capability import EntityType

_EXPECTED = {
    "role_template",
    "model",
    "human",
    "external_agent",
    "company",
    "skill",
    "tool",
}


def _check_values(sqltext: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sqltext))


@pytest.mark.unit
def test_python_enum_matches_expected() -> None:
    assert set(typing.get_args(EntityType)) == _EXPECTED


@pytest.mark.unit
def test_orm_check_matches_enum() -> None:
    # Match by sqltext (the name is rewritten by the naming-convention template).
    constraints = [
        c
        for c in CapabilityCardRow.__table__.constraints
        if "entity_type" in str(getattr(c, "sqltext", ""))
    ]
    assert constraints, "entity_type CHECK constraint not found on ORM model"
    assert _check_values(str(constraints[0].sqltext)) == _EXPECTED


@pytest.mark.unit
def test_migration_0019_matches_enum() -> None:
    mig = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / ("0019_capability_entity_type_union.py")
    )
    text = mig.read_text(encoding="utf-8")
    union = re.search(r"_UNION\s*=\s*\(([^)]*)\)", text, re.DOTALL)
    assert union, "could not locate _UNION in migration 0019"
    assert _check_values(union.group(1)) == _EXPECTED
