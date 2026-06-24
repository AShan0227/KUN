"""Audit F053: ORM CHECK constraints match migrations 0011/0012 exactly.

Migrations 0011 (RSI spine) and 0012 (task_checkpoints) declared ~23 CHECK
constraints, but the corresponding ORM Row classes declared none — a systematic
ORM↔migration drift. The constraints are now mirrored into __table_args__. This
test parses the migration source and asserts, per table, that the ORM CHECK set
{name: sqltext} equals the migration CHECK set, so the two cannot drift again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from kun.core.orm import Base
from sqlalchemy import CheckConstraint

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# (migration file, table names it creates that we mirror)
_MIGRATION_TABLES = {
    "0011_rsi_data_spine.py": [
        "runtime_capabilities",
        "runtime_experiments",
        "strategy_search_requests",
        "diagnostic_records",
        "goal_anchors",
        "plan_reviews",
        "evidence_ledger",
    ],
    "0012_task_checkpoints.py": ["task_checkpoints"],
}


def _migration_checks(filename: str) -> dict[str, dict[str, str]]:
    """{table_name: {constraint_name: sqltext}} parsed from a migration source."""
    src = (_VERSIONS / filename).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "create_table" or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        table = first.value
        checks: dict[str, str] = {}
        for arg in node.args[1:]:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "CheckConstraint"
                and arg.args
                and isinstance(arg.args[0], ast.Constant)
            ):
                name = next(
                    (
                        kw.value.value
                        for kw in arg.keywords
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                    ),
                    None,
                )
                assert name, f"{table}: CheckConstraint without a name in {filename}"
                checks[name] = arg.args[0].value
        if checks:
            out[table] = checks
    return out


def _orm_checks(table_name: str) -> dict[str, str]:
    # Base's MetaData naming convention prepends ``ck_<table>_`` to the explicit
    # constraint name. Migrations 0011/0012 wrote literal names *without* that
    # prefix (unlike 0003/0008, which did follow it), so strip the convention
    # prefix to compare the logical constraint name + predicate faithfully.
    table = Base.metadata.tables[table_name]
    prefix = f"ck_{table_name}_"
    out: dict[str, str] = {}
    for c in table.constraints:
        if isinstance(c, CheckConstraint) and c.name:
            name = c.name[len(prefix):] if c.name.startswith(prefix) else c.name
            out[name] = str(c.sqltext)
    return out


_ALL = [(fn, t) for fn, tables in _MIGRATION_TABLES.items() for t in tables]


@pytest.mark.unit
@pytest.mark.parametrize("filename,table", _ALL, ids=[t for _, t in _ALL])
def test_orm_check_constraints_match_migration(filename: str, table: str) -> None:
    migration = _migration_checks(filename).get(table, {})
    assert migration, f"no CHECK constraints parsed for {table} in {filename}"
    orm = _orm_checks(table)
    assert orm == migration, (
        f"ORM↔migration CHECK drift for {table}:\n"
        f"  only in migration: { {k: migration[k] for k in migration.keys() - orm.keys()} }\n"
        f"  only in ORM:       { {k: orm[k] for k in orm.keys() - migration.keys()} }\n"
        f"  text mismatch:     "
        f"{ {k: (migration[k], orm[k]) for k in migration.keys() & orm.keys() if migration[k] != orm[k]} }"
    )
