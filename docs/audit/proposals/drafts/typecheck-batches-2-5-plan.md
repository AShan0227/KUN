# Typecheck debt — batches 2–5 execution plan (DRAFT)

> Status: **needs-review** (Loop-2 TIER-2 #6). Continues [typecheck-debt.md](../typecheck-debt.md). Batch 1 already landed (mypy 145→116, commit 8a1006b). No code here — this is the execution plan to reach 0 and flip the mypy CI gate to hard.
> Author: audit fix-loop · Date: 2026-06-24

## 1. Current state (measured `uv run mypy kun`, 2026-06-24)
**116 errors.** After batch 1 cleared the zero-risk mechanical noise (12 unused-ignore + 17 type-arg), the remainder is:

| count | code | nature |
|---|---|---|
| 31 | `arg-type` | wrong argument type — **may be real bugs** |
| 21 | `attr-defined` | attribute doesn't exist — real bug or missing stub |
| 18 | `no-untyped-def` | function missing annotations — mechanical |
| 10 | `no-any-return` | returns Any — tighten return type |
| 8 | `dict-item` | dict literal value type mismatch |
| 7 | `misc` | mixed |
| 5 | `call-overload` | no matching overload — **may be real bug** |
| 4 | `prop-decorator` / 4 `assignment` / 2 `union-attr` / 2 `comparison-overlap` / 1 each `type-var`/`no-untyped-call`/`no-redef`/`index` | tail |

**Hotspots**: `game_production.py` 27, `cli.py` 24, `mission_director.py` 13, `feature_activation_audit.py` 9, `daemon.py` 7 — top 2 = 51/116 (44%).

## 2. Batches (each its own PR; gate = mypy count strictly down + full unit green + no new `# type: ignore`)

### Batch 2 — mechanical annotations (~28: 18 no-untyped-def + 10 no-any-return)
- Add parameter/return annotations to untyped functions; tighten `no-any-return` to the real return type.
- **Risk**: annotating sometimes surfaces a hidden inconsistency — confirm each is not a real bug rather than slapping `Any` on it.
- **Verify**: mypy 116→~88; unit green; ruff clean; zero new ignores.

### Batch 3 — hotspot sweep (`cli.py` 24 + `game_production.py` 27 = 51)
- Concentrated pass on the two files that are 44% of the debt; biggest single-PR win.
- Note: `game_production.py` is also slated to move to `kun/domains/` (ADR-028) — coordinate so this typing work isn't thrown away (either do it after the move, or keep it minimal since the file relocates). **Flag for human sequencing.**
- **Verify**: mypy down by ~51 in those files; unit green.

### Batch 4 — semantic errors (~40: 31 arg-type + 21 attr-defined + 5 call-overload + 2 union-attr + 2 comparison-overlap; overlaps hotspots)
- These are **not** "add an annotation" fixes — they're actual call/attribute mismatches. **Treat each as a suspected real bug**; add/extend a unit test that exercises the path, fix the underlying mismatch.
- **Hard rule**: do NOT silence with `# type: ignore` — that just relocates the debt (the very thing batch 1 cleaned). An ignore is acceptable only with a cited external-stub reason.
- **Verify**: each fix has a test; mypy down; unit green; no new ignores.

### Batch 5 — close-out + flip the gate
- Clear the residual tail (dict-item, misc, prop-decorator, assignment, type-var, no-redef, index) to **0**.
- Flip `.github/workflows/ci.yml` typecheck job from soft (`continue-on-error: true`) to a **hard gate** (mypy non-zero fails CI), sitting alongside the coverage gate (F133/G10) and `alembic check` (F052).
- **Verify**: `uv run mypy kun` exits 0; CI typecheck job no longer `continue-on-error`; full suite green.

## 3. Milestone
116 → ~88 (B2) → ~37 (B3) → ~handful (B4) → **0 + hard gate** (B5). Each batch independently shippable and green-gated.

## 4. Risk / sequencing
- **Sequencing with ADR-028**: batch 3's `game_production.py` work overlaps the planned domainization move — a human should decide order (move-then-type, or type-minimal-now). Flagged.
- **Risk**: batch 4 semantic fixes are where real bugs hide — slower, test-gated, not mechanical. Budget accordingly.
- **Rollback**: each batch is a self-contained PR; revert independently. The gate flip (B5) is the last step and only after 0 is reached, so it can never make CI red on landing.

## 5. Covers / relates
G09 (typecheck debt). Batch 1 landed (8a1006b). Relates to F133/G10 (coverage gate) + F052 (alembic check) — together they form the "CI gate enforcement" epic. Coordinate batch 3 with ADR-028 (game_production domainization).
