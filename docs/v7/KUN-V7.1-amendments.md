# KUN V7.1 — Amendments from X.A → X.O Implementation Waves

> **Why this doc exists**: V7.0 (docs/v7/KUN-V7.md) describes the
> protocol's principles. The X.A → X.O implementation waves discovered
> 14 operational specifics that V7.0 doesn't cover (verified via
> `grep` — 0 hits on each new symbol in V7.md). This file enumerates
> every gap and provides the exact text to merge.
>
> **How to use this doc**: For each section below, find "§X.Y" pointer
> → V7.md location → "INSERT" / "REPLACE" text → merge into V7.md when
> the merge cycle is approved.
>
> **Status**: amendments staged, awaiting user-driven merge into V7.0.
>
> Date: 2026-05-29 · Source waves: X.A through X.O (commits cef3767 → e9c5fc3)

---

## Table of Amendments

| ID | V7.0 § target | Title | Severity |
|---|---|---|---|
| A1 | §4.3 | EngineeringDiscipline runtime wiring | P0 |
| A2 | §11.4 | Ensemble cost multiplier real-LLM evidence | P1 |
| A3 | §12.2 | TicketVerifier Protocol (CANARY → PRODUCTION) | **P0** |
| A4 | §12.3 | RSI write-side ProcessAudit closure (X.M) | P1 |
| A5 | §12.4.2 | Trifecta past line `bug_root_cause_cases` lookup | P1 |
| A6 | §12.5 (new) | MethodologyRuntimeSelector read-side + methodology_to_gate_bridge write-side | **P0** |
| A7 | §15.2 | Gate R6 production-path-reachability rule | **P0** |
| A8 | §15.3 (new) | Production-entry diff check + ProductionEntryDiffChecker | P1 |
| A9 | §16.2 | 6 anti-patterns ⇒ 6 + 5 root causes R1-R5 | **P0** |
| A10 | §16.3 | PRODUCTION_ENTRIES.md inventory + LongTaskRuntimeBundle pattern | **P0** |
| A11 | §16.5 | runtime_features_used trace shape | P1 |
| A12 | §16.6 | Angle 8 production-path-traceability + hidden-orphan-audit template | **P0** |
| A13 | §16.8 | X.H AST CI + X.N template lint as required automation | P1 |
| A14 | §20 | Cockpit reader pattern + discipline_store + ensemble reader | P1 |
| A15 | §23.2 | Acceptance: AST CI + template lint pass | P1 |
| A16 | Appendix A | X.A → X.O phase entries | P2 |
| A17 | Appendix B | New terms (hidden orphan / chain-reach / etc.) | P2 |

---

## A1 — §4.3 Add EngineeringDiscipline runtime wiring

**V7.0 §4.3** lists 10 Claude Code disciplines but doesn't say HOW they are
enforced at runtime. X.I-0a wired `EngineeringDisciplineEnforcer` into
`LongTaskOrchestrator` at run completion, and X.O added the
`discipline_store` cache so cockpit can surface failed disciplines.

**INSERT after current §4.3 table**:

```markdown
**Runtime enforcement (X.I-0a)**: `kun.governance.engineering_discipline.
EngineeringDisciplineEnforcer` runs at each long-task completion via
`LongTaskOrchestrator` (when env `KUN_V7_DISCIPLINE_ENFORCER_ENABLED=true`).
Failed disciplines are emitted as `long_task.discipline_report` events,
recorded in process-local cache `kun.api.discipline_store` (X.O — was
returning hardcoded `[]` until X.O), and surfaced via cockpit endpoint
`/cockpit/discipline/recent`. Future X.P will migrate cache → PG.
```

---

## A2 — §11.4 Add ensemble cost multiplier real-LLM evidence

**V7.0 §11.4** describes `ensemble_invoke` API but never references the
cost multiplier empirics.

**INSERT after §11.4 API table**:

```markdown
**Cost multiplier — real LLM empirics (V7.1 added from X.D-2 dogfood v12)**:

| 配置 | V7 §12.4.4 估值 | 真 LLM 实测 (Anthropic Haiku) |
|---|---|---|
| 全 trifecta (3 lines, real ensemble baseline) | 5-6x | **5.16x** ✅ |
| Single-LLM baseline | 1.0x | $0.00008 / call |

Source: `scripts/dogfood_v12_real_trifecta_checkpoint_collab.py` (commit `52142e4`).
This is the first empirical validation of the §12.4.4 estimate.
```

---

## A3 — §12.2 TicketVerifier Protocol (P0 security)

**V7.0 §12.2** says CANARY → PRODUCTION needs `user_approval_ticket_id`.
**V7.0 didn't specify the verifier**, so the original lifecycle service
accepted any non-empty string. X.H.TICKET-VERIFY fixed this with a
`TicketVerifier` Protocol + 12-test attacker matrix.

**REPLACE V7.0 §12.2 acceptance rule for production flip**:

```markdown
### §12.2 Production flip (V7.1 — X.H.TICKET-VERIFY upgraded)

CANARY → PRODUCTION 必须 ALL OF:

  1. user_approval_ticket_id (non-empty string)
  2. **The ticket exists in the InMemoryCollaborationQueue** (or future
     PG-backed queue)
  3. ticket.status ∈ {"answered", "fallback_selected"}
  4. response.selected_option == "approve"

Enforced by `kun.governance.capability_lifecycle.TicketVerifier`
Protocol; production wiring is
`kun.integration.collab_ticket_verifier.InMemoryQueueTicketVerifier`.
Without a verifier wired, the lifecycle service falls back to legacy
truthy-check for backward compat. **Production callers MUST wire the
verifier**.

**Attacker matrix (V7.1 acceptance, X.H 12 tests)**:
  - fake ticket id → DENIED
  - status ∈ {open, waiting, escalated, cancelled, closed} → DENIED
  - answered with selected_option='hold' → DENIED
  - fallback_selected with fallback='hold' → DENIED
  - fallback_selected with fallback='approve' → ALLOWED
  - genuinely answered approve → ALLOWED
  - verifier raises → fail-closed (CapabilityLifecycleError)
```

---

## A4 — §12.3 RSI write-side ProcessAudit closure

**V7.0 §12.3** mandates three evidences (strategy_replay_report +
process_audit + capability_candidate) for CANDIDATE → REPLAY. X.D-1
shipped 3 v11 seeds via that flow. X.H.META cp'd 2 more seeds without
going through it. X.M retroactively closed the RSI write-side gap.

**INSERT note at end of §12.3**:

```markdown
**V7.1 RSI write-side self-loop closure (X.M)**: V7.0 §12.3 enforces
three-evidence rule on capability candidates **from external dogfood
runs**. But methodology distill candidates from KUN's own dev_logs were
historically cp'd directly into `seeds/methodologies/`. X.M (commit
`02659bd`) closes the loop: every methodology candidate MUST also go
through ProcessAudit + StrategyReplayReport before merging. Live
example: `docs/dist-output/seeds-new/v12-xh/` retroactively audits the
2 X.H-distilled seeds.

This makes the RSI loop **self-auditing**: KUN's own methodology
distill workflow is subject to the same V7 §12.3 acceptance gate as
any other capability source.
```

---

## A5 — §12.4.2 Trifecta past line `bug_root_cause_cases` lookup

**V7.0 §12.4.2** mentions past line should consult `bug_root_cause_cases`
but doesn't specify the lookup mechanism. X.I-4 shipped it.

**INSERT after §12.4.2**:

```markdown
**Past-line implementation (X.I-4)**: `kun.integration.bug_root_cause_lookup.
lookup_similar_root_cause_cases` queries the alembic 0013
`bug_root_cause_cases` table (`BugRootCaseRow`), scores rows by keyword
overlap with `recent_steps` summaries, returns top-K findings. The
production trifecta coordinator's past hook (built by
`LongTaskRuntimeBundle._build_real_llm_trifecta_coordinator`) tries DB
first (0 LLM cost). Falls back to a small LLM call only when DB has no
matches.
```

---

## A6 — §12.5 (new) MethodologyRuntimeSelector + methodology_to_gate_bridge

**V7.0 §12** describes RSI loop but does NOT specify:
- The read-side: how promoted methodologies reach next-task LLM (X.G)
- The write-side: how methodology candidates enter Gate (X.I-0b)

Both are now wired.

**INSERT new §12.5**:

```markdown
### §12.5 RSI loop wiring (V7.1 — X.G + X.I-0b)

**Read-side (X.G — promoted methodologies reach next-task LLM)**:

  promoted yaml in `seeds/methodologies/`
      ↓ `kun.engineering.methodology_runtime_loader.load_methodologies`
      ↓ `MethodologyRuntimeSelector.select_for(TaskContext, top_k=3)`
      ↓ `render_for_system_prompt` (engineering-first keyword overlap scoring)
      ↓ appended to LongTaskOrchestrator system prompt
      ↓ LLM真 sees methodology in conversation
      ↓ emits `long_task.methodology_injected` event for cockpit

Env: `KUN_V7_METHODOLOGY_INJECT_ENABLED=true` + `KUN_V7_METHODOLOGY_TOP_K=N`.

**Write-side (X.I-0b — methodology candidates enter Gate)**:

  `MethodologyDistillStep.run` (in `kun.engineering.idle_batch`,
   triggered by `idle_batch_worker` launched from `kun/api/main.py:152`)
      ↓ for each novel candidate from `methodology_distill.distill()`
      ↓ `kun.integration.methodology_to_gate_bridge.
         admit_methodology_candidate_via_gate`
      ↓ `GateService.admit` with synthesized payload
      ↓ if approve: chain to `capability_lifecycle_v7_bridge` → lifecycle_transitions row
      ↓ chain to `auditor_report_v7_bridge` → auditor_reports row

Before X.I-0b, GateService.admit was only called from
`kun/integration/prompt_ab.py` (A/B test framework, not user-facing path).
X.I-0b makes the entire V7 §15 lifecycle WRITE SIDE production-active
via the existing daemon.

Env: `KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED=true` (default).
```

---

## A7 — §15.2 Gate R6 production-path-reachability rule

**V7.0 §15.2** has Gate rules R1-R4 (test_report / diagnostic / debrief
/ self_referential). X.I-2 added R6.

**INSERT new R6 row in §15.2 Gate rules table**:

```markdown
| R6 | **production_reachability** (V7.1, X.I-2) | When `experiment.kind ∈ {runtime, capability}`, target_module must be reachable from at least one entry in `docs/PRODUCTION_ENTRIES.md` (verified via `kun.governance.production_path_traceability.check_symbol_reachable`). methodology kind always passes. Legacy callers without kind set pass through unaffected. | reject if R6 fails on runtime kind |
```

---

## A8 — §15.3 (new) Production-entry diff check

**V7.0 §15** says capability candidates declare what they'll change.
Until X.I-3 the declaration field didn't exist; X.I-3 added it; X.I-3-FIX
+ X.O wired the consumer.

**INSERT new §15.3**:

```markdown
### §15.3 Production-entry diff check (V7.1, X.I-3-FIX + X.O)

`TaskSpec.production_entry_changes_required: list[str]` (added X.I-3)
declares which production-entry files a task intends to modify.

At run completion (X.I-3-FIX), `LongTaskOrchestrator` calls
`kun.governance.production_entry_diff_check.ProductionEntryDiffChecker.
check_against_actual(declared, actual)` and emits
`long_task.production_entry_diff` event with verdict ∈ {match, drift,
no_declaration, undeclared_changes}.

`actual` paths are sourced (X.O) by walking the executor's final_messages
for tool_calls in the known-writer set (self_reflect.write / file_io.write
/ Edit / Write) and extracting `path` args via
`kun.engineering.extract_changed_paths.extract_changed_paths_from_messages`.

The production WS entry (`kun/engineering/orchestrator.py`) emits a
**postrun** event with the real actual paths so subscribers see the
true verdict (X.O — before X.O the pre-completion event always saw
actual=[]).

V7.1 acceptance: any LLM that declares production_entry_changes_required
but actually changes nothing → verdict='drift' → MD MissionAlignmentReview
verdict turns drifting (future X.Q wiring).
```

---

## A9 — §16.2 expand 6 anti-patterns to 6 + 5 root causes

**V7.0 §16.2** lists 6 anti-patterns from Claude Code retros. X.H
self-audit found 5 ADDITIONAL root causes that LLMs (including me)
repeatedly commit at the AUDIT step itself.

**INSERT after §16.2 6-anti-pattern table**:

```markdown
### §16.2.5 5 hidden-orphan root causes (V7.1, X.H)

The 6 anti-patterns above describe failure shapes in the IMPLEMENTATION.
X.H self-audit (2026-05-29) found 5 ADDITIONAL root causes that cause
LLMs (Claude, gpt, Qwen) and humans to MISS the anti-patterns when
auditing. Documented in `docs/dev_logs/X.H-self-audit-rootcause.md`.

| # | Root cause | KUN defense |
|---|---|---|
| **R1** | grep-verify granularity wrong: `import X` ≠ runtime use of X | `docs/templates/hidden-orphan-audit-prompt.md` §4 Step 4 + Step 7 chain-reach; `kun.governance.production_path_traceability.check_symbol_reachable` |
| **R2** | No production-entries inventory: "where is production?" unanswered | `docs/PRODUCTION_ENTRIES.md` mandate |
| **R3** | opt-in defaults OFF + no consumer enforcement = silent orphan | `LongTaskRuntimeBundle` pattern + `EXPECTED_BUNDLE_KEYS` CI audit |
| **R4** | Test fixture caller syntactically == production caller | AST test that ONLY parses production-entry files (`tests/integration/test_production_entry_runtime_bundle.py`) |
| **R5** | Retrospective skips entry-level grep | Audit template §7 banned phrases + §6 mandatory meta self-audit |

Every retrospective MUST verify safe=true for all 5 root causes
before claiming a feature done. Template provides structured JSON
output with `root_cause_check.{R1..R5}.safe: bool`.
```

---

## A10 — §16.3 PRODUCTION_ENTRIES.md inventory + LongTaskRuntimeBundle pattern

**V7.0 §16.3** lists current entries informally in a table. X.H formalized
the inventory + enforcement.

**REPLACE V7.0 §16.3 current entries table with**:

```markdown
**Canonical inventory**: `docs/PRODUCTION_ENTRIES.md` (V7.1, X.H R2).

Every blessed production entry must:
  1. Appear in the inventory file with file path + purpose
  2. Be parsed by `tests/integration/test_production_entry_runtime_bundle.py`
     AST audit
  3. For LongTaskOrchestrator-using entries, MUST spread
     `**runtime_bundle.as_orchestrator_kwargs()` OR explicitly name every
     opt-in feature

**`LongTaskRuntimeBundle` pattern (V7.1, X.H)** — single hub for opt-in
runtime features:
  - All opt-in `LongTaskOrchestrator` ctor params go through the bundle
  - `LongTaskRuntimeBundle.as_orchestrator_kwargs()` projects to ctor kwargs
  - `from_env_defaults()` factory reads env switches consistently
  - `enabled_flags` reports actual activation vs precondition-missing
  - CI test `EXPECTED_BUNDLE_KEYS` set enforces no opt-in feature ships
    without going through the bundle

Without the bundle, three releases (X.E trifecta, X.G methodology, DIST-D
critique) shipped class-level opt-in features that the production WS entry
silently omitted, making them runtime orphans. The bundle makes this
impossible to repeat.
```

---

## A11 — §16.5 runtime_features_used trace shape

**V7.0 §16.5** mandates trace but doesn't specify the field shape.
X.H.TRACE shipped it.

**INSERT after §16.5 trace requirement**:

```markdown
**V7.1 trace shape (X.H.TRACE)**:

```python
TaskCheckpoint.working_state["runtime_features_used"] = {
    "methodologies": [{"title": str, "topic": str, "score": float, "file_path": str}],
    "trifecta_ticks": [{"call_count": int, "past_state": str, "present_state": str,
                       "future_state": str, "n_findings": int,
                       "total_cost_usd": float, "any_line_failed": bool}],
    "discipline_report": {"overall_score": float, "failed_disciplines": list[str]},
    "production_entry_diff": {"verdict": str, "has_drift": bool,
                              "declared_count": int, "actual_count": int},
}
```

Plumbed through `ExecutorLoop.runtime_features_provider` callback. Any
PG `task_checkpoints` row can be queried to answer "which features
influenced this task?" — closes V7 §16 cause #6 (no forced trace).
```

---

## A12 — §16.6 Angle 8 + audit prompt template + Step 7 chain-reach

**V7.0 §16.6** lists 7 audit angles. X.I-1 added Angle 8. X.N shipped a
reusable audit prompt template. X.O upgraded the template with Step 7
chain-reach.

**INSERT after §16.6 7-angle list**:

```markdown
**Angle 8 — Production-path traceability (V7.1, X.I-1)**:

For each claimed capability, the auditor must verify it's reachable from
at least one entry in `docs/PRODUCTION_ENTRIES.md`. If only reachable
from `tests/` or `scripts/dogfood_*`, the capability is **nominal-wired
but actual orphan** — must have `risk_level ≥ P1`, `allow_release=false`,
and `must_fix` includes "wire to production entry".

Implementation auto-computes reachability via
`kun.governance.production_path_traceability.check_symbol_reachable`
and feeds the result to the LLM auditor render (`render_auditor_prompt`
production_path_check arg). The heuristic auditor also auto-escalates
when target_module is symbol-shape but not reachable.

**Audit prompt template (X.N)**: `docs/templates/hidden-orphan-audit-prompt.md`
is the reusable §1-§8 prompt any LLM can run on any codebase.
Structural lint guards: `tests/unit/test_hidden_orphan_audit_template.py`.
Driver script for any-LLM execution: `scripts/run_audit_prompt_on_capability.py`.

**Step 7 chain-reach (V7.1.1, X.O)**: Template §4 originally had a R1
granularity bug — direct symbol grep missed chain wiring. X.O added
Step 7: walk callers of the symbol → check if any caller's host file
is a production entry → 2-hop terminates as chain-wired (not orphan).
Mandatory §6 self-audit must confess R1 false positives caught by Step 7.
```

---

## A13 — §16.8 X.H AST CI + X.N template lint as required automation

**V7.0 §16.8** lists acceptance criteria but doesn't mandate the
automation. X.H + X.N shipped CI guards.

**INSERT in §16.8 acceptance criteria**:

```markdown
**V7.1 automation requirements**:

  - `tests/integration/test_production_entry_runtime_bundle.py` MUST pass
    (8 tests). This is THE CI guard against R1/R5 recurrence — AST audit
    over production entry file ensures bundle plumbing.
  - `tests/unit/test_hidden_orphan_audit_template.py` MUST pass (8 tests).
    Ensures the audit template structure doesn't silently drift.
  - For every "approved" capability in V7 §16.8, the audit-prompt template
    + driver script SHOULD have been run at least once (manual or CI),
    with the response stored in dev_logs.
```

---

## A14 — §20 Cockpit reader patterns

**V7.0 §20** describes cockpit panels but doesn't specify the reader
layer. X.B + X.F + X.O shipped them.

**INSERT after §20 panel design table**:

```markdown
**V7.1 cockpit reader layer**:

| Endpoint | Source | Reader function | Wave |
|---|---|---|---|
| /cockpit/capabilities | `lifecycle_transitions` PG table | `list_recent_lifecycle_transitions` | X.B + X.F |
| /cockpit/missions/{task_id}/alignment | `mission_alignment_reviews` | `list_recent_mission_reviews` | X.B |
| /cockpit/supervisor/auditor-reports | `auditor_reports` | `list_recent_auditor_reports` | X.B |
| /cockpit/ensemble/recent | `ensemble_calls` | `list_recent_ensemble_calls` | X.F |
| /cockpit/discipline/recent | `kun.api.discipline_store` (process-local cache, X.P → PG) | `list_recent_discipline_reports` | X.O |
| /cockpit/writes-status | meta — per-table writer + bridge env status | `_writes_wired_status` | X.B.MF-3 |

All readers return `ReaderResult` with `error_kind` ∈ {db_connection_refused,
table_not_found, permission_denied, session_scope_failure, unknown_db_error,
None} so cockpit can distinguish "tenant truly has no data" vs "DB
unreachable" (X.B.MF-6).

Frontend `/cockpit` page (frontend/src/app/cockpit/page.tsx) auto-refreshes
every 8s and renders 6 panels (X.F + X.K real-browser-verified).
```

---

## A15 — §23.2 Acceptance: AST CI + template lint pass

**V7.0 §23.2** acceptance criteria need updates for X.H/X.N automation.

**INSERT new acceptance rows in §23.2**:

```markdown
| **AST production-entry audit pass** (V7.1, X.H) | `tests/integration/test_production_entry_runtime_bundle.py` 8/8 green; production WS entry spreads `**bundle.as_orchestrator_kwargs()` |
| **Audit prompt template structural lint** (V7.1, X.N) | `tests/unit/test_hidden_orphan_audit_template.py` 8/8 green |
| **All 8 V7 mechanisms env all-on real fire** (V7.1, X.L) | `scripts/dogfood_v15_all_8_mechanisms_on.py` exits 0; enabled_flags 4/4 True; 5 event types fire |
```

---

## A16 — Appendix A: X.A → X.O phase entries

**INSERT after V7.0 Phase G entry** (end of Appendix A current content):

```markdown
### Phase X.A → X.O: post-V7.0 implementation waves (delivered)

| Phase | Title | Status | Key commit |
|---|---|---|---|
| X.A | qi/ + nuo/ rename + re-export | ✅ | (commit history) |
| X.B (MF-1..MF-6) | 4 X.B tables + bridges + writes_wired_status | ✅ | `cef3767` etc |
| X.C (MF-AR-LLM..DOGFOOD-V11) | Real LLM auditor + crash resume + trifecta class + lifecycle walker + collab e2e + capstone | ✅ | `b262624`, `2c957ab` etc |
| X.D | ProcessAudit + real-LLM dogfood v12 (5.16x multiplier) | ✅ | `0416bc4`, `52142e4` |
| X.E | Trifecta wiring into LongTaskOrchestrator + dogfood v13 | ✅ | `9341c5c`, `2bb3589` |
| X.F | Cockpit daily-use UI + ensemble reader | ✅ | `979dd19` |
| X.G | Methodology read-side runtime selector + dogfood v14 | ✅ | `bb55e6a` |
| X.H | self-audit 5 root causes + bundle + ticket verify + trace + META | ✅ | `5270750`, `71e13c0`, `b7cb07b`, `8cb3711` |
| X.I-0 | Mechanism orphan fix (Gate / Auditor / Discipline) | ✅ | `132b948` |
| X.I-1..4 | Auditor Angle 8 + Gate R6 + TaskPlan field + Trifecta past | ✅ | `00e3afc` |
| X.I-3-FIX | Half-orphan diff checker consumer | ✅ | `510ebe1` |
| X.J | Real user real task ≥30 min | ⏳ pending (awaiting user) |
| X.K | Cockpit browser verify | ✅ | `d419cac` |
| X.L | Dogfood v15 8 mechanisms env all-on | ✅ | `e976476` |
| X.M | ProcessAudit X.H seeds | ✅ | `02659bd` |
| X.N | CI lint of audit template + driver script | ✅ | `c8a670b` |
| X.O | 2 self-audit-caught bugs + Step 7 chain-reach | ✅ | `e9c5fc3` |
| X.P (this doc) | V7.1 amendments | ⏳ in progress |
```

---

## A17 — Appendix B: new terms

**INSERT in V7.0 Appendix B glossary**:

```markdown
| **hidden orphan** | Capability nominally wired (in class signature / tested / referenced) but production entry never instantiates or calls it. KUN's 5-release recurrence rate before X.H. |
| **chain-reach** | Indirect production wiring via 1+ caller hops. Step 4 direct grep misses; Step 7 explicit walk catches (X.O). |
| **production entry** | File listed in `docs/PRODUCTION_ENTRIES.md`. Audit / Gate / template check reachability against this set. |
| **LongTaskRuntimeBundle** | Single hub for opt-in `LongTaskOrchestrator` features. CI guards against new opt-in skipping the bundle. |
| **bundle.enabled_flags** | Dict mapping {trifecta, methodology, critique, discipline} → bool. Reports real activation (not just "ctor param was set"). |
| **TicketVerifier** | Protocol that resolves `user_approval_ticket_id` against InMemoryCollaborationQueue and asserts status ∈ {answered, fallback_selected} + selected_option=='approve' (V7 §12.2). Without verifier wired, lifecycle accepts any non-empty string (legacy fallback only). |
| **Angle 8** | Production-path-traceability axis of V7 §16.6 attacker audit. Added X.I-1. |
| **R1-R5 root causes** | Five LLM-recurrent audit failures (grep granularity / no inventory / opt-in no consumer / fixture-as-prod / module-not-entry). See `docs/dev_logs/X.H-self-audit-rootcause.md`. |
| **methodology runtime selector** | `MethodologyRuntimeSelector.select_for(TaskContext, top_k)` — read-side of RSI loop. Loads `seeds/methodologies/*.yaml`, scores by keyword overlap, injects top-K into LLM system prompt. |
| **production_entry_diff_check** | Compares `TaskSpec.production_entry_changes_required` (declared) vs actual paths from tool_calls (extracted via `extract_changed_paths_from_messages`). Verdict ∈ {match, drift, no_declaration, undeclared_changes}. |
```

---

## Merge procedure recommendation

The amendments above are non-destructive — every INSERT is additive
and every REPLACE preserves V7.0 semantics while adding V7.1 specifics.
Recommended merge order:

  1. P0 first: A3 (TicketVerifier), A6 (RSI wiring), A7 (Gate R6),
     A9 (5 root causes), A10 (PRODUCTION_ENTRIES + bundle), A12 (Angle 8 + template)
  2. P1 next: A1, A2, A4, A5, A8, A11, A13, A14, A15
  3. P2 last: A16, A17

Each merge MUST be accompanied by an updated entry in
`docs/dev_logs/LT-progress.md` per ADR-025.

After all amendments merged, V7.0's "End of Document" line in
`docs/v7/KUN-V7.md:2235` SHOULD be updated to read:

  > 本文档是后续 KUN 开发的唯一对齐锚点 (V7.1 amendments 见
  > docs/v7/KUN-V7.1-amendments.md, X.P 起所有 X.* phase 必须先 merge
  > amendments 再写代码).

---

## Self-meta note

V7.0 ships 2241 lines of careful protocol. X.A → X.O ships ~15,000 lines
of implementation that the protocol doesn't fully cover. The gap is
not "V7 wrong" — it's "V7 directionally correct but operationally
underspecified". This amendments doc closes the gap.

The amendments themselves were discovered by running the audit prompt
template I shipped in X.N on V7.md itself (using `grep` for each
expected term). Zero hits on 13/14 X.A-O concepts confirms the gap is
real, not speculative.
