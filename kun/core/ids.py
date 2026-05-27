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
    "action",  # act-
    # L5 治理层 (ADR-021 / ADR-022 / ADR-024)
    "goal_anchor",  # ga-  · GoalAnchor (ADR-022)
    "diagnostic",  # dx-  · DiagnosticRecord (ADR-021)
    "plan_review",  # pr-  · PlanReview (ADR-022)
    "evidence",  # ev_l-· EvidenceLedger entry (ADR-024)
    "strategy_search",  # ss-  · StrategySearchRequest (ADR-024)
    "experiment_run",  # er-  · runtime_experiments (ADR-024)
    "capability_promo",  # cp-  · runtime_capabilities (ADR-024)
    "task_checkpoint",  # tcp- · TaskCheckpoint (LT.C, ADR-022 持久化)
    "bug_case",  # bc-  · BugRootCase (alembic 0013, RCDH 案例库)
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
    # L5 治理层
    "goal_anchor": "ga",
    "diagnostic": "dx",
    "plan_review": "pr",
    "evidence": "ev_l",
    "strategy_search": "ss",
    "experiment_run": "er",
    "capability_promo": "cp",
    "task_checkpoint": "tcp",
    "bug_case": "bc",
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
