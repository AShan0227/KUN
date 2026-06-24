# RSI mainline wiring — stepwise landing plan (DRAFT)

> Status: **needs-review** (Loop-2 TIER-2 #3). Operationalizes [rsi-mainline-wiring.md](../rsi-mainline-wiring.md) into a build-ready, staged plan. No code here.
> ADR-024 north star: "each run makes itself a little smarter." Today: all 5 ring engines exist with unit tests, but the drivetrain between them isn't installed in production — event flow doesn't reach the detector, synthetic evidence feeds the gate, spine tables don't flow.
> Author: audit fix-loop · Date: 2026-06-24

## 0. Hard prerequisites (do NOT start ring wiring before these)
1. **ADR-027 persistence** ratified + landed — the spine tables (0011/0012) must be a real read/write substrate with CAS, or wiring just moves the demo-only problem into prod.
2. **ADR-028 single canonical runtime** — pick the `agents/` supervisor/strategist/gate/mission-director as canonical; otherwise we wire into a runtime that a second one shadows (F036/F142).
3. **De-hardcode gate scores (F008/F016/F017)** — the gate must consume real TestReport/debrief evidence, not `pass_rate=1.0` / `evidence_quality=0.75` literals, or ring 4 "passes" are theater.

## 1. The closed loop (ADR-024) and its 7 break points
detect → strategize → safe-experiment → gate-land → influence-next. Each ring's break (verified, from rsi-mainline-wiring.md §1):
- **R1 detect (F039/F022)**: `SupervisorService.observe()` only instantiated in `scripts/e2e_rsi_demo.py`; no production consumer feeds it; the DB `emitter` is never injected. (LLMRouter *does* emit `llm.fallback.triggered` to the events table — but nothing forwards it to `observe()`.)
- **R2 strategize (F040)**: Strategist doesn't read `strategy_search_requests`.
- **R3 experiment (F021/F022)**: the experiment ring (runtime_experiments) is essentially absent in prod.
- **R4 gate-land (F041)**: the only production gate path feeds hardcoded `pass_rate=1.0`; `GateService()` is built without a `capability_writer`, so approve doesn't write `runtime_capabilities`.
- **R5 influence-next (F041/F042)**: spine tables demo-only; `diagnostic_records`/`goal_anchors`/`evidence_ledger` ~0 production writes.
- **R-enable (F089)**: `enable_capability` self-referential gate is bypassable + has no production caller (must land fail-closed + real token when wired).
- **R-input (F025)**: WS `task_state` is dead code → long-task routing never runs.

## 2. Per-ring landing detail + "wired" acceptance assertion
For each ring the **definition of done = the table/signal has real production read+write, proven by an integration test asserting flow** (not just a unit test that the engine works).

| ring | wire | acceptance assertion (integration, real PG/NATS) |
|---|---|---|
| R1 | NATS/outbox consumer calls `observe()`; inject DB `emitter` that writes `strategy_search_requests` | publish an anomaly event → a row appears in `strategy_search_requests` for the tenant |
| R2 | Strategist polls/subscribes `strategy_search_requests` (status=open) → produces candidates | seed a request → Strategist consumes it (status→claimed) + emits candidates |
| R3 | candidates → `runtime_experiments` (shadow first), Executor applies change_spec, Tester emits TestReport | a candidate runs as a shadow experiment → row lifecycle pending→running→done |
| R4 | gate consumes **real** TestReport/debrief (post F008/F016/F017); inject `capability_writer` | a passing experiment → `runtime_capabilities` row written (not synthetic) |
| R-enable | wire enable path with authoritative metadata + real approval token; F089 fail-closed | enable w/o valid token → PermissionError; with valid token → enabled=true row |
| R5 | `evidence_ledger`/`diagnostic_records`/`goal_anchors` get real writers; `runtime_capabilities.enabled` read by the next decision | a full loop leaves a contiguous evidence trail; next task reads enabled capability |
| R-input | populate WS `task_state` so `handle_long_task_input` 6-bucket routing runs | a long-task follow-up message routes (not "task already running") |

## 3. Ready-but-unwired bypass components (rsi-mainline §2b) — where each attaches
- **F063 RedisAssetStore** → wire as the Context asset substrate during R5 (depends ADR-027).
- **F064 CANARY→PROD approval validator** → wire into R4 promotion path.
- **F065 evidence_ledger** stub → real read/write in R5; upgrade F155 contract test assertions.
- **F101 ResourceQuota/ExplorationPenalty** → inject into the production Strategist at R2 (F146 already threads real tenant).
- **F103 proactive Layer 1a** → make it actually dispatch (don't just mark seen) when the proactive layer goes live.
- **F104 PromptABService** → call public API + give it a production caller at R5 (reads enabled capability).
- **F087 SupervisorPool** → if used, filter checks per dimension + dedup; prove a production caller first.
- **F090 MultiJudge** → see §5 (decorrelation).
- **F099/F114 L6 eval** → wire into production metrics or archive explicitly.

## 4. Shadow / canary rollout
- R3 safe-experiments run **shadow-only first** (compute + record, no production effect) for ≥1 release; promote to canary (small sampling_rate) only after shadow metrics look sane.
- Each ring lands behind its existing `KUN_V7_*` flag (all default-off today — F119); enable per-tenant, watch, widen.

## 5. F090 — MultiJudge decorrelation (LLM facts per claude-api skill)
`multi_judge.py` calls **one model at a fixed `temperature=0.1`, N times** → ballots are correlated, so the "independent majority vote" assumption is false. **Per the claude-api skill (authoritative):** `temperature`/`top_p` are **removed on Opus 4.7/4.8 and Fable 5** (passing them 400s), so "vary the temperature to decorrelate" is **not possible** on current models. Decorrelation must come from **distinct models** (e.g. an Opus judge + a Fable judge + a local-model judge) or **distinct prompt perspectives** (different rubric/role per judge). Until that lands, do not claim "independent majority vote" — describe it honestly as "single-model multi-sample self-consistency." Wire the real panel when the quality signal feeds the gate (R4).

## 6. Integrity red line
Until a ring is genuinely wired (its acceptance assertion passes in prod), PROGRESS.md / decisions.md must NOT claim that ring "已闭环/已达成" — continue the honest-annotation style already applied in F047/F049/F050/F069. "RSI 真闭合" is claimable only when R1→R5 all pass their acceptance assertions.

## 7. Milestones + acceptance
- **M1 (substrate)**: ADR-027 + ADR-028 landed; F008/F016/F017 done. Gate: spine tables read/written with CAS; one canonical runtime.
- **M2 (detect→strategize)**: R1+R2 wired. Gate: anomaly → request → candidate, end-to-end integration test.
- **M3 (experiment→gate)**: R3 shadow + R4 real-evidence + R-enable fail-closed. Gate: passing shadow experiment writes a real capability row; enable requires a valid token.
- **M4 (influence + bypass components)**: R5 + §3 components + R-input. Gate: a full loop leaves a contiguous evidence trail and the next decision reads enabled capability.
- **M5 (honesty close)**: only now update PROGRESS/decisions to "RSI closed-loop achieved", with the L5 note (F069) updated accordingly.

## 8. Covers / relates
Operationalizes F021/F022/F025/F039/F040/F041/F042 (+ §2b F063/F064/F065/F087/F089/F090/F099/F101/F103/F104/F114). Prereqs: **ADR-027 → ADR-028 → this**. Honesty per F047/F049/F050/F069. Flags per F119.
