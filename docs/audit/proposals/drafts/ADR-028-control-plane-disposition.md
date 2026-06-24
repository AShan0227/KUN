# ADR-028 (DRAFT) · Disposition of `kun/control_plane/`: keep, dissolve, or hybrid

> Status: **needs-review** — decision draft for human ratification (Loop-2 TIER-2 #2). Resolves the architectural fork behind **F036 / F038**; relates to F012/F037/F070-F072/F112/F113/F142.
> This ADR's job is to **make a decision**: it lays out the options, weighs them, and gives a recommendation for a human to ratify. It does not write code.
> Author: audit fix-loop · Date: 2026-06-24

## 1. Background

- **ADR-020 said `kun/control_plane/` should disappear** after the L1 reorg, its functions redistributed across the 5-layer directory. Instead it grew to **~45k LOC across 41 files (~50% of `kun/`)** — the single largest package.
- **Two disconnected runtimes**: `kun/agents/` (the ADR-020 role stack) and `kun/control_plane/` (the V6 runtime) have **zero module-level imports** between them — bridged only by 2 function-local lazy imports (`kun_runtime_runner.py:430`, `daemon.py:809`). The platform runs two mutually-unaware runtimes with duplicate implementations (`control_plane/supervisor.py` vs `agents/supervisor/service.py`; `control_plane/mission_director.py` vs `agents/mission_director/service.py`) and dual entry points (cli → control_plane, api → engineering). (F036/F142)
- **`daemon.py` is a 6,930-line god-module** (F012/F113) mixing engine, ~20 governance passes, worker pool, watchtower bridge, plus hardcoded product playbooks.
- **Product scripts welded into the platform layer**: `game_production.py` (7,907 LOC) is ~60% one game's embedded TS/React source + brittle string-replace patches (F037/F070-F072).
- **Governance chain broken** (F038): `decisions.md` stops at ADR-026; V6/V7 (Control Plane revival, Mission Director, trifecta) have no ADR. So this very decision has no recorded basis — which is the gap this ADR closes.
- **In progress already**: `docs/CONTROL_PLANE_DOMAINS.md` started the split — 6 game-production template modules (~4.8k LOC) moved to `control_plane/templates/`, and the core runtime no longer reverse-imports the rainflow domain. So the *direction* is set; this ADR ratifies and completes it.

## 2. Decision options

### Option A — Dissolve per ADR-020 (delete `control_plane/`, redistribute to 5 layers)
- **For**: honors the accepted ADR-020; ends the dual-runtime split by forcing everything into the role-stack layers.
- **Against**: a 45k-LOC teardown of the *currently-authoritative* runtime (V6 is what actually runs). Highest blast radius, highest regression risk, longest freeze. The V6 mission/work-item state machine is real and working — dissolving it to chase a paper architecture is high-cost/low-immediate-value.

### Option B — Bless V6 `control_plane` as first-class, amend ADR-020
- **For**: lowest churn; documents reality (V6 is the live runtime).
- **Against**: leaves the god-module (F012/F113), the product-welding (F037), and the dual-runtime duplication (F142) in place — it legitimizes the mess instead of fixing it. Doesn't end the `agents/`↔`control_plane/` schism.

### Option C — Hybrid (RECOMMENDED): `control_plane` = orchestration layer; extract domains + split daemon; reconcile the two runtimes
- Keep `control_plane/` as a **thin platform orchestration layer** (mission/work-item state machine, execution contracts, ledger, supervisor protocol — the ~12k "core V6" in CONTROL_PLANE_DOMAINS.md).
- **Extract product runners** (game_production, rainflow_ad, frontier50, game-design-research, external-sample) to `kun/domains/<product>/`; the embedded source/patches become product-repo assets, not platform code.
- **Split `daemon.py`** along engine / governance-plugins / product-playbook seams.
- **Reconcile the two runtimes**: pick ONE supervisor / mission-director implementation (the `agents/` ones are the ADR-020 canonical stack) and have `control_plane` orchestrate *them* — deleting the duplicate `control_plane/{supervisor,mission_director}.py`, ending the zero-import schism (F142/F036).
- **For**: matches the in-flight CONTROL_PLANE_DOMAINS roadmap; incremental + shippable; fixes the real debt (god-module, welding, duplication) without a high-risk teardown; preserves the working V6 runtime.
- **Against**: requires amending ADR-020 (control_plane survives, redefined) — a deliberate doc change, not a silent drift.

## 3. Recommendation

**Adopt Option C.** It is the lowest-regret path that actually resolves F036/F037/F012/F142 rather than papering over them, and it ratifies the direction `docs/CONTROL_PLANE_DOMAINS.md` already took. Concretely: redefine `control_plane` in an amended ADR-020 as the **platform orchestration layer**, make `agents/` the canonical role implementations it drives, and move all product specifics to `kun/domains/`.
**Requires human ratification** of: (a) Option C over A/B, and (b) that `agents/` (not `control_plane/`) holds the canonical supervisor/mission-director.

## 4. Work required regardless of A/B/C
1. **Domainize products**: game_production / rainflow / frontier50 → `kun/domains/<product>/`; v32–v55 patch assets out of platform code (F037/F070-F072).
2. **Split `daemon.py`** into engine / governance-plugins / product-playbook modules (F012/F113); delete shadowed dup functions.
3. **Backfill ADRs** for V6/V7 (Control Plane revival, Mission Director, trifecta, Qi/Nuo agentization) — retroactive, dated (F038); reconcile PROGRESS/decisions/PROGRESS_VS_PLAN/PROMISES.
4. **End the zero-import schism** (F142/F036): one runtime drives the other; delete duplicates.
5. **Institutionalize**: extend X.Q production-entry check to "every new component declares a production entry + an ADR".

## 5. Stepwise landing + per-step verification
| step | change | verify |
|---|---|---|
| 0 | Ratify this ADR + amend ADR-020 (doc) | human sign-off recorded in decisions.md |
| 1 | Add **import-linter** contracts (NEW dev dep) | CI fails if `control_plane`↔`domains` or `core`→upper-layer edges appear |
| 2 | Move product runners → `kun/domains/<product>/` | import-linter: no domain import from platform core; unit suite green |
| 3 | Choose canonical supervisor/MD; delete duplicate | grep: one impl; integration green; no behavior regression |
| 4 | Split `daemon.py` by seam | each module < ~1.5k LOC; import-linter layer contract; green |
| 5 | Backfill ADRs + reconcile progress docs | decisions.md ADR chain contiguous; F038 closed |

## 6. Risks / rollback
- **Risk**: moving runners breaks dynamic imports / entry points → do it behind the domain protocol, one product at a time, with the unit+integration suite as gate; revert per-product.
- **Risk**: deleting a duplicate runtime drops a behavior the other lacked → diff the two impls first; port gaps before deleting.
- **Rollback**: steps 2–4 are per-module and independently revertible; step 0/5 are docs.
- **Dependency**: sequence AFTER ADR-027 persistence (the state substrate) and alongside the RSI wiring (which needs a single canonical runtime to wire into).

## 7. Covers / relates
Resolves the **F036/F038** fork (decision + governance chain); directs **F012/F037/F070/F071/F072/F112/F113/F142** remediation in architecture-debt.md. New tooling: **import-linter** (dev dep) to enforce the chosen layering. Prereq ordering: **ADR-027 → ADR-028 → RSI wiring.**
