# X.H Self-Audit — 5 Root Causes & Closed-Loop Fixes

> User invoked V7 §16.6 attacker-audit pattern on 2026-05-29:
> "方案能力 → 模块实现 → 生产入口 → 真实数据 → 验收测试 这五层没有闭合.
> 这部分你在自检一下."
>
> Self-audit confirmed all 6 causes the user named. Causes #2-#6 mapped to
> the 5 root causes documented here.

## TL;DR

| Symptom | Root cause | X.H fix | Commit |
|---|---|---|---|
| `trifecta_coordinator` / `methodology_selector` / `critique_every_n_steps` existed as ctor params but production WS entry omitted all three | **R1, R2, R3, R4, R5** | `LongTaskRuntimeBundle` + AST-audit CI test | `5270750` |
| `validate_transition` accepted any non-empty string as `user_approval_ticket_id` — cancelled / fake / hold tickets all "passed" | **R3, R5** | `TicketVerifier` Protocol + verifier check + 12 attacker tests | `71e13c0` |
| No way to ask "which methodologies / trifecta ticks influenced this task" from a checkpoint row | **R5** | `runtime_features_provider` callback + `working_state.runtime_features_used` | `b7cb07b` |
| Self-audit methodology drift: I shipped "Closes X.E" / "Closes X.G" without grep'ing the production entry | **R1, R5** | Process retrofit yaml + new seed + production-entries inventory | (this commit) |

## The 5 root causes

### R1 — grep-verify granularity wrong (the methodology itself encoded the error)

X.B.MF-1 introduced the rule "commit '接到 X' claim 前必须 grep 验证非测试
代码 import 了 X". For MF-1 specifically (V6 Mission Director → V7 bridge),
that grep was sufficient because the import IS the wiring. But for X.E
(trifecta) and X.G (methodology selector), the import in
`long_task_orchestrator.py` was a **class-level type hint** for an opt-in
ctor parameter, NOT a runtime instantiation. Both pass the same grep, but
only one actually fires on the WS production path.

**Lesson**: "X imported by non-test file" is a primitive proxy for
"production code uses X". The correct grep is "X **instantiated** at the
known production entry". The X.B audit methodology was at the wrong
granularity for X.E / X.G use.

**Fix shipped this wave**:
- New seed `production_path_wiring_audit_before_claim.yaml` (the long-
  promised retrofit) explicitly mandates grep-ing **instantiation /
  invocation at a known production entry**, not just module import.
- New seed `opt_in_feature_must_be_consumed_at_production_entry.yaml`
  generalizes the lesson — every opt-in feature must have a known
  consumer at a known production entry, audited by automation.
- CI test `tests/integration/test_production_entry_runtime_bundle.py`
  parses the production entry with AST and asserts `**bundle.kwargs()`
  is spread (or all keys explicitly named). Future opt-in features
  cannot be silently orphaned.

### R2 — no production entries inventory

Before X.H there was no document, no yaml, no test that said "KUN's
production entries are: WS handler at kun.api.ws / REST at … / cron at …".
Each new LongTaskOrchestrator opt-in shipped without anyone consulting a
checklist of "which call sites must also be updated".

**Fix shipped this wave**:
- `docs/PRODUCTION_ENTRIES.md` lists all blessed production entry paths
  (currently 1 — WebSocket in `kun/engineering/orchestrator.py:1312`).
  Update procedure documented: add new entry → add to this doc → add
  to CI audit.
- CI audit `tests/integration/test_production_entry_runtime_bundle.py`
  references the inventory, parses `orchestrator.py` for the
  LongTaskOrchestrator call site, validates bundle plumbing.

### R3 — opt-in defaults OFF without "must be consumed somewhere" enforcement

Cost-sensitive defaults OFF is fine in principle (don't surprise users with
LLM bills). But there was no automated "feature X exists 60 days, never
opt-in in any production entry — likely orphan" alarm.

**Fix shipped this wave**:
- `LongTaskRuntimeBundle.enabled_flags` reports which opt-ins were
  requested but couldn't activate (precondition missing). Cockpit can
  surface "requested but unwired" honestly.
- AST audit test fails the build if a future opt-in feature is added
  to the orchestrator class without going through the bundle.

### R4 — test fixtures look like production callers

When `test_trifecta_hook_fires_every_n_steps` writes
`LongTaskOrchestrator(..., trifecta_coordinator=coord, ...)`, it's
syntactically indistinguishable from a production caller. So the test
passing gave false confidence that the feature was wired.

**Fix shipped this wave**:
- The AST audit test explicitly walks `kun/engineering/orchestrator.py`
  (the production entry) and only that file. Test files are excluded by
  design — they can pass arbitrary kwargs without affecting the audit.
- New seed `opt_in_feature_must_be_consumed_at_production_entry.yaml`
  warns explicitly: "the test that constructs the orchestrator does NOT
  count as a consumer. Production entry is a specific, registered
  call site."

### R5 — module-level retrospective skips entry-level check

X.E retrospective said "wiring done" because the class signature
changed and grep found a non-test import. The retrospective writing
process didn't include "go to orchestrator.py and confirm the call site
now passes this new param". Three commits over X.E / X.G / DIST-D
repeated the same pattern.

**Fix shipped this wave**:
- New seed mandates a "production-entry grep" line in every commit
  message that claims "接到生产". The seed text gives the exact grep:
  `grep -n "FEATURE_NAME" kun/engineering/orchestrator.py` and warns
  that a successful match must be at a *real call site*, not a comment.
- LT-progress.md template update (forthcoming): every X.* retrospective
  section must include a "production entry evidence" subsection with
  the grep output pasted.

## Concrete impact (numbers before / after X.H)

| Layer | Before X.H | After X.H |
|---|---|---|
| Production WS entry calls | 7 explicit kwargs, 0 opt-in features | 7 kwargs + `**bundle.kwargs()` (covers trifecta + methodology + critique) |
| `user_approval_ticket_id` validation | non-empty string check | full V7 §12.2 verifier (ticket exists + status answered + selected approve) |
| Attacker matrix coverage | 0 tests | 12 tests (fake id / wrong status × 5 / hold response / fallback approve / fallback hold / happy / verifier explodes / legacy compat) |
| Checkpoint causality | working_state empty | working_state.runtime_features_used carries methodologies + trifecta_ticks |
| Test count | 2093 | 2117 (+24) |

## Process improvements landing this wave (commit X.H.META)

1. `docs/PRODUCTION_ENTRIES.md` (new) — single source of truth for
   blessed production entries.
2. `seeds/methodologies/production_path_wiring_audit_before_claim.yaml`
   (new) — the X.B promise made good (was never actually shipped).
3. `seeds/methodologies/opt_in_feature_must_be_consumed_at_production_entry.yaml`
   (new) — the X.H learning, generalized.
4. CI test (already shipped in X.H-1) — automation guard for R1, R2, R5.

## What X.H does NOT fix (deferred to X.I)

- **真用户真任务跑**: still 0. X.H makes the wiring real, but no
  customer has used it yet.
- **Cockpit UI 真用**: page renders, but no user has driven decisions
  through it.
- **L6 content distribution adapters**: still deferred per user.

## Self-meta note

Writing this doc was uncomfortable. The 5 root causes pin down that the
methodology I shipped (X.B.MF-1's grep-verify) was at the wrong
granularity, and I repeated the failure mode across three commits. The
fix isn't just "be more careful" — it's "automate the audit so being
careful isn't required". CI now enforces what culture failed to.
