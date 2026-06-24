# ADR-027 (DRAFT) · Persistence redesign: from file/in-memory state to a consistent DB-backed store

> Status: **needs-review** (draft for human ratification — Loop-2 TIER-2). Resolves audit **F019**; unblocks the concurrency / multi-replica family.
> Supersedes the file-store + InMemoryControlPlane state model where they hold authoritative runtime state.
> Author: audit fix-loop · Date: 2026-06-24

## 1. Problem statement

KUN's runtime state today lives in two demo-grade substrates that cannot survive production:

- **`kun/control_plane/file_store.py`** — a single-file JSON snapshot. Every `put_*` does load-all → mutate → serialize-all → `os.replace` (**O(N²)** write amplification, F019). Cross-process consistency is "last writer wins": two daemons / an API replica + a daemon overwrite each other (F011, F015). The ledger has no sequencing, so concurrent appends collide (F014). Unknown fields are silently dropped on load (F019).
- **`kun/api/control_plane.py` `InMemoryControlPlane`** — all state in process memory + local JSON; no tenant isolation, no cross-process coherence, daemon state arbitrarily overwritable (F093).

Compounding resource/concurrency findings that all trace to this substrate: artifact growth without bound/TTL (F074), `mission.ledger_refs` O(N²) inline rewrite (F081), in-process-only lock granularity (F062, F147), no cross-process slot/lease (F010, F076), supervisor/cluster state in memory (F085, F088).

**Consequence:** the documented invariant "production = single daemon + worker_pool=1" (concurrency-multi-replica.md) is the only thing keeping this correct. That ceiling blocks horizontal scale and makes a crash lose in-flight state.

## 2. Goals / non-goals

**Goals**
- Authoritative runtime state in **Postgres** with **ADR-007 RLS** tenant isolation.
- **Optimistic concurrency** (per-row version / CAS) so concurrent writers cannot silently clobber.
- **Append-only ledger** with monotonic sequence per (tenant, stream) — no lost or reordered events.
- Bounded growth: artifact refs paged / TTL'd, no full-object rewrites on single-record writes.
- Explicit **read/write consistency boundaries** (what must be read-your-writes vs eventually-consistent).
- A migration path that lets file-store and DB coexist during rollout (no big-bang cutover).

**Non-goals**
- Not introducing a new datastore (Redis already exists for locks/leases per F010/F085; Postgres already exists). No Kafka/event-bus dependency.
- Not redesigning the domain models themselves — only their persistence + concurrency envelope.
- Not solving multi-region; single-region multi-replica is the target.

## 3. Design

### 3.1 Storage
- Each control-plane record type (missions, work_items, plans, gates, ledger, daemon-service state, supervisor state) becomes a Postgres table (most already exist or mirror the 0011/0012 spine). All carry `tenant_id` + RLS policy (reuse the 0011 `tenant_isolation` pattern).
- **Version column** `row_version BIGINT NOT NULL DEFAULT 0` (or `xmin`-based) on every mutable row. Writes are `UPDATE ... SET ..., row_version = row_version + 1 WHERE pk = :pk AND row_version = :expected`; 0 rows affected ⇒ `StaleWriteError` → caller reloads + retries (bounded). This is the CAS the concurrency proposal calls for.
- **Append-only ledger**: `evidence_ledger` / event streams get `sequence BIGINT` assigned by a per-(tenant, stream) sequence (DB sequence or `SELECT ... FOR UPDATE` on a counter row). Appends never rewrite siblings (kills F014 + F081 O(N²)).
- **Artifacts**: store refs in a child table keyed by (tenant, mission_id, created_at) with an index + retention policy (TTL / keep-last-N), not inlined into the mission row (kills F074/F081 growth).

### 3.2 Concurrency model
- **In-process**: keep `asyncio.Lock` only for short in-memory critical sections (cf. F147 — already narrowed). DB CAS is the cross-process source of truth.
- **Cross-process slots/leases**: daemon slot claim (F076) and holder locks (F010/F085) move to the existing Redis lease (Lua CAS, atomic acquire/renew/release per F085 draft) — Postgres CAS for data, Redis for short-lived leadership/leases.
- **Consistency boundaries**: a worker reading its own mission state gets read-your-writes (same tx / same row CAS). Dashboards / API list views are explicitly **eventually consistent** (may lag a tick) — documented, not pretended-consistent (addresses F015's "API serves a stale copy" honestly).

### 3.3 Unknown-field preservation (F019 part 2)
- Add an `extra JSONB` side column (or `model_config extra="allow"` + envelope) so fields not in the current Pydantic model are **preserved** across load→persist, and log `warning` when extras are seen. Removes the silent-drop failure mode during schema skew.

## 4. Migration path (no big-bang)
1. **Shadow-write**: introduce the DB store behind a `PersistenceBackend` protocol; control_plane writes to BOTH file-store (authoritative) and DB (shadow). Compare in CI/staging.
2. **Backfill**: one-time loader file-store JSON → DB rows (idempotent, re-runnable).
3. **Flip reads**: switch reads to DB behind `KUN_PERSISTENCE_BACKEND=db` (default still file); dogfood under the flag.
4. **Flip authoritative**: DB becomes source of truth; file-store demoted to optional export/debug.
5. **Remove file-store** authoritative paths once a release has run clean on DB.

Each step is independently shippable and reversible by flag.

## 5. Impact on the 8 spine tables + control_plane
- The 0011/0012 spine tables already exist with CHECK parity (F053 landed). This ADR makes them **actually read/written by production** (today most are demo-only — see rsi-mainline-wiring §2b) — so it must be sequenced WITH the RSI wiring (ADR-RSI draft, TIER-2 #3): persistence is the substrate, RSI wiring is the first real consumer.
- `InMemoryControlPlane` (F093) becomes a thin cache over the DB store with RLS-scoped reads; daemon state (`KUN_V6_DAEMON_STATE`) moves to a `daemon_service` table with CAS so a second daemon can't silently overwrite.

## 6. Stepwise landing + per-step verification
| step | change | verify (assertion) |
|---|---|---|
| 1 | `PersistenceBackend` protocol + DB impl behind flag | unit: backend conforms; integration(PG): round-trip a record |
| 2 | version/CAS column + StaleWriteError + retry | integration: two writers, one gets StaleWriteError, no lost update (covers F011) |
| 3 | append-only ledger w/ sequence | integration: concurrent appends → contiguous sequence, no loss (F014) |
| 4 | artifact child table + TTL | integration: N artifacts → mission row size constant; TTL purges (F074/F081) |
| 5 | shadow-write + backfill + read flip | staging: file vs DB diff == 0 over a dogfood run |
| 6 | flip authoritative + drop single-writer constraint | load test: 2 daemons + worker_pool>1, no corruption |

## 7. Risks / rollback
- **Risk**: CAS retry storms under contention → bound retries + jittered backoff; metric on StaleWriteError rate.
- **Risk**: backfill drift → idempotent loader + shadow-diff gate before flip.
- **Rollback**: each step flag-guarded; revert the flag to fall back to file-store (authoritative until step 4).
- Until step 6 ships clean, **keep the single-daemon + worker_pool=1 production constraint** documented in concurrency-multi-replica.md.

## 8. Acceptance criteria
- No `put_*` triggers a full-store rewrite (O(N) writes for N records, verified by a write-count test).
- Two concurrent writers never lose an update (CAS-verified integration test).
- Ledger appends are contiguous + lossless under concurrency.
- Unknown fields survive load→persist (or at minimum log a warning).
- Production can run ≥2 daemons / worker_pool>1 without state corruption.

## 9. Covers / relates
Resolves **F019**; substrate for **F010/F011/F014/F015/F062/F074/F076/F081/F085/F088/F093/F147** (concurrency-multi-replica.md) and is the prerequisite for **rsi-mainline-wiring** (spine tables become live). Sequence: **ADR-027 (this) → RSI wiring → drop single-writer constraint.**
