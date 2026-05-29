# KUN Production Entries — single source of truth

> Created 2026-05-29 as part of V7 Phase X.H self-audit (R2 fix).
> Every blessed production entry that drives long tasks lives here.
> If a feature needs to be available to real users, it must be wired
> into **at least one** entry on this list.

## Why this document exists

Before X.H, three releases (X.E, X.G, DIST-D) shipped opt-in
LongTaskOrchestrator features that **looked wired** (class signature
accepted them, unit tests passed, dogfood scripts exercised them) but
the actual user-facing entry never consumed them. The X.H self-audit
named "no production entries inventory" as one of the 5 root causes.

This file is that inventory. A test
(`tests/integration/test_production_entry_runtime_bundle.py`) audits the
entries here at CI time, asserting that they spread
`LongTaskRuntimeBundle.as_orchestrator_kwargs()` or explicitly name every
opt-in feature.

## The list (current)

| # | Entry | Path | Purpose | Bundle wired? |
|---|---|---|---|---|
| 1 | **WebSocket task entry** | `kun/engineering/orchestrator.py` (method `run_long_task_branch`, LongTaskOrchestrator instantiation around line 1330) | Real user sends a task via `/ws`; orchestrator branches to `LongTaskOrchestrator.run_long_task` | ✅ since X.H.PROD-ENTRY-WIRE (commit `5270750`) |

## Adding a new entry

When a new production entry is added (REST API, cron daemon, scheduled
sweeper, batch worker, etc), the engineer must:

1. **Add the entry to the table above** with its file path and purpose.
2. **Pass `**runtime_bundle.as_orchestrator_kwargs()`** when constructing
   LongTaskOrchestrator at the new entry. Use
   `LongTaskRuntimeBundle.from_env_defaults(...)` to build the bundle.
3. **Update the CI audit test**:
   - `tests/integration/test_production_entry_runtime_bundle.py` —
     extend `test_production_entry_passes_runtime_bundle_kwargs` to
     also walk the new entry's file with AST and assert the same
     guarantee.
4. **Append a "production entry evidence" subsection** to the wave's
   retrospective (X.E.* style) showing the grep output for the bundle
   spread.

## What counts as a production entry (R4 fix)

A **production entry** is:
- a call site that the deployed `kun-api` process can reach in response
  to a real user / scheduled trigger
- registered here in the table
- audited by the CI test linked above

**Not** a production entry (do not register, do not audit):
- test files (`tests/**`) — fixture callers don't count
- dogfood scripts (`scripts/dogfood_v*.py`) — manual demos don't count
- one-off admin scripts that are run by hand

## What "wired" means (R3 fix)

The bundle's `enabled_flags` field tracks **runtime activation**, not just
construction:

- `enabled_flags["trifecta"] = True` means env switch ON **and**
  `llm_router` was available **and** the coordinator successfully built.
- `enabled_flags["methodology"] = True` means env switch ON **and**
  `seeds/methodologies/` was non-empty **and** the selector built.
- `enabled_flags["critique"] = True` means env switch with N>=1 **and**
  `external_supervisor` was wired.

If any precondition is missing, the bundle reports `False` honestly so
the cockpit can show "requested but precondition missing" rather than
pretending the feature is active.

## Related methodologies

- `seeds/methodologies/production_path_wiring_audit_before_claim.yaml`
- `seeds/methodologies/opt_in_feature_must_be_consumed_at_production_entry.yaml`
- `seeds/methodologies/grep_verify_before_assume.yaml`
- `seeds/methodologies/production_loop_real_pg_e2e_chain.yaml`
- `docs/dev_logs/X.H-self-audit-rootcause.md` — the 5 root causes this
  document addresses.
