"""V7 Phase X.I-3-FIX — consumer for TaskSpec.production_entry_changes_required.

Before X.I-3-FIX, X.I-3 was a half-orphan: the
``TaskSpec.production_entry_changes_required`` field was defined in the
pydantic schema and tests asserted it round-trips, but **no production
code ever read it back to check anything**. I (Claude) caught this
myself by running the audit prompt template I had just written
(``docs/templates/hidden-orphan-audit-prompt.md``) on X.I-3.

This module closes the loop:

  TaskSpec.production_entry_changes_required   (declared by Director)
       ↓
  ProductionEntryDiffChecker.check_against_actual(declared, actual)
       ↓
  ProductionEntryDiffReport (frozen IO)
       ↓
  long_task.production_entry_diff event (cockpit + audit trail)
       ↓
  If mismatch: MissionAlignmentReview verdict drifts (downstream)

The checker is intentionally pure (no I/O): it compares the declared
list against an injected ``actual_changed_paths`` list. Callers
(LongTaskOrchestrator at run completion) decide how to materialize the
actual list — could be ``git diff --name-only``, the executor's tool-
call ledger, or a Director-provided expected_changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kun.core.logging import get_logger

log = get_logger("kun.governance.production_entry_diff_check")


@dataclass(frozen=True)
class ProductionEntryDiffReport:
    """V7 §13.6 frozen IO — outcome of one production-entry diff check."""

    declared: list[str] = field(default_factory=list)
    actual: list[str] = field(default_factory=list)
    declared_but_unchanged: list[str] = field(default_factory=list)
    changed_but_undeclared: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    verdict: str = "match"  # match / drift / no_declaration / no_actual

    @property
    def has_drift(self) -> bool:
        return bool(self.declared_but_unchanged) or bool(self.changed_but_undeclared)


class ProductionEntryDiffChecker:
    """Compare declared production entry changes against actual changes.

    Production wiring:
      - LongTaskOrchestrator at run completion (X.I-3-FIX). The orchestrator
        reads ``task_ref.spec.production_entry_changes_required`` and pulls
        the actual-changed-paths from a caller-provided source (git diff /
        tool-call ledger). If those differ, the orchestrator emits
        ``long_task.production_entry_diff`` with the report.
      - Future: Mission Director can chain this into MissionAlignmentReview
        verdict (drifting when has_drift=True).
    """

    @staticmethod
    def check_against_actual(
        declared: list[str] | None,
        actual: list[str] | None,
    ) -> ProductionEntryDiffReport:
        decl_set = {p.strip() for p in (declared or []) if p and p.strip()}
        actual_set = {p.strip() for p in (actual or []) if p and p.strip()}

        if not decl_set and not actual_set:
            return ProductionEntryDiffReport(
                declared=[],
                actual=[],
                verdict="no_declaration",
            )
        if not decl_set:
            # Director didn't declare anything but files DID change at
            # production entries — could be intentional (refactor under
            # the hood). Caller decides how to treat. We emit verdict
            # "changed_but_undeclared" to flag it.
            return ProductionEntryDiffReport(
                declared=[],
                actual=sorted(actual_set),
                changed_but_undeclared=sorted(actual_set),
                verdict="undeclared_changes",
            )
        if not actual_set:
            # Director declared we'd touch X but nothing actually changed.
            # The wiring claim ("I'll接 X to production entry") was never
            # honored. This is the exact X.H/X.I-3 failure mode.
            return ProductionEntryDiffReport(
                declared=sorted(decl_set),
                actual=[],
                declared_but_unchanged=sorted(decl_set),
                verdict="drift",
            )

        matched = decl_set & actual_set
        declared_but_unchanged = decl_set - actual_set
        changed_but_undeclared = actual_set - decl_set
        verdict = (
            "match"
            if not declared_but_unchanged and not changed_but_undeclared
            else "drift"
        )
        return ProductionEntryDiffReport(
            declared=sorted(decl_set),
            actual=sorted(actual_set),
            matched=sorted(matched),
            declared_but_unchanged=sorted(declared_but_unchanged),
            changed_but_undeclared=sorted(changed_but_undeclared),
            verdict=verdict,
        )


__all__ = [
    "ProductionEntryDiffChecker",
    "ProductionEntryDiffReport",
]
