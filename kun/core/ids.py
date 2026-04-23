"""ULID-based ID generation.

Per §13.1 TASK.md 字段规则: task_id 用 ULID（时间序）而不是 UUID, 便于排序和归档.
This module provides prefixed ULIDs for different entity types for readability.
"""

from __future__ import annotations

from typing import Final, Literal

from ulid import ULID

EntityKind = Literal[
    "task",  # tk-
    "role_inst",  # ri-
    "role_tpl",  # rt-
    "skill",  # sk-
    "memory",  # mm-
    "handoff",  # hp-
    "runtime",  # rs-
    "capability",  # cc-
    "event",  # ev-
    "score",  # sc-
    "experiment",  # ex-
    "notification",  # nt-
    "rule",  # rl-
]

_PREFIX: Final[dict[EntityKind, str]] = {
    "task": "tk",
    "role_inst": "ri",
    "role_tpl": "rt",
    "skill": "sk",
    "memory": "mm",
    "handoff": "hp",
    "runtime": "rs",
    "capability": "cc",
    "event": "ev",
    "score": "sc",
    "experiment": "ex",
    "notification": "nt",
    "rule": "rl",
}


def new_id(kind: EntityKind) -> str:
    """Create a new prefixed ULID id.

    Example:
        >>> new_id("task")
        'tk-01HK0...'

    The prefix makes debugging much easier than raw UUIDs.
    """
    prefix = _PREFIX[kind]
    return f"{prefix}-{ULID()}"


def parse_kind(ident: str) -> EntityKind | None:
    """Extract entity kind from a prefixed id."""
    if "-" not in ident:
        return None
    prefix, _ = ident.split("-", 1)
    for kind, p in _PREFIX.items():
        if p == prefix:
            return kind
    return None
