"""ULID-based ID generation.

Per §13.1 TASK.md 字段规则: task_id 用 ULID（时间序）而不是 UUID, 便于排序和归档.
This module provides prefixed ULIDs for different entity types for readability.
"""

from __future__ import annotations

from threading import Lock
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
    "action",  # act-
    "relationship",  # rel-
    # V2.1 additions
    "sd",  # StrategyDecision (§17.7)
    "tp",  # TaskPanorama (§13.8)
    "aa",  # AttentionAnchor (§13.7 / §18.8)
    "es",  # EmergentSolution (§13.9)
    "preheat",  # ContextPreheat
    "patch",  # PanoramaPatch (§7.7)
    "diag",  # DiagnoseRun (§10.6)
    "decision",  # V4 DecisionTicket
    "anchor",  # alias for aa
    "incident",  # IncidentResponse event
    "mission",  # Long-horizon mission
    "milestone",  # Mission milestone
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
    "action": "act",
    "relationship": "rel",
    # V2.1
    "sd": "sd",
    "tp": "tp",
    "aa": "aa",
    "es": "es",
    "preheat": "ph",
    "patch": "pat",
    "diag": "diag",
    "decision": "dt",
    "anchor": "aa",
    "incident": "inc",
    "mission": "msn",
    "milestone": "mile",
}
_MAX_ULID_INT: Final[int] = (1 << 128) - 1
_id_lock = Lock()
_last_ulid_int_by_kind: dict[EntityKind, int] = {}


def new_id(kind: EntityKind) -> str:
    """Create a new prefixed ULID id.

    Example:
        >>> new_id("task")
        'tk-01HK0...'

    The prefix makes debugging much easier than raw UUIDs.
    """
    prefix = _PREFIX[kind]
    return f"{prefix}-{_monotonic_ulid(kind)}"


def _monotonic_ulid(kind: EntityKind) -> ULID:
    candidate = int(ULID())
    with _id_lock:
        previous = _last_ulid_int_by_kind.get(kind)
        if previous is not None and candidate <= previous:
            candidate = previous + 1
        if candidate > _MAX_ULID_INT:
            raise OverflowError("ULID space exhausted")
        _last_ulid_int_by_kind[kind] = candidate
        return ULID.from_int(candidate)


def parse_kind(ident: str) -> EntityKind | None:
    """Extract entity kind from a prefixed id."""
    if "-" not in ident:
        return None
    prefix, _ = ident.split("-", 1)
    for kind, p in _PREFIX.items():
        if p == prefix:
            return kind
    return None
