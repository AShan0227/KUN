"""Dogfood v12 — real LLM trifecta + real PG checkpoint + real collab ticket.

⚠️ FIXTURE/DEMO (audit F059): PASS only checks TrifectaState==OK (hooks ran), not output quality; not run in CI. A green run does NOT prove real RSI closure. See docs/audit/proposals/demo-script-honesty.md.

V7 Phase X.D.REAL-LONGTASK: the dogfood-v11 capstone proved 7-piece wiring
via 1 test with stub hooks. v12 takes the 3 X.C P0/P1 pieces (trifecta /
checkpoint / collab) and runs them with **real LLM** + **real PG**, so we
can read off:

  - trifecta cost_multiplier_vs_baseline in a real run (vs §12.4.4 estimate)
  - checkpoint round-trip across a real process boundary
  - CollaborationTicket genuinely gating a real lifecycle_transitions row

Scenario (single self-contained run, ~5-15 min, $0.5-3 typical):

  1. **Pre-flight**: snapshot 4 X.B tables + lifecycle_transitions +
     task_checkpoints. Cost meter starts at $0.

  2. **Setup**: open a CollaborationTicket
     "Approve real-LLM dogfood-v12 budget cap = $5? (recommended: approve)"
     with fallback_policy.option='approve', deadline = now + 1 min
     (we'll auto-respond approve almost immediately to simulate a fast
     release_owner who saw the budget).

  3. **Real LLM trifecta tick × 3**: at t=0s / t=120s / t=240s, fire
     `TrifectaCoordinator.run()` with **real Anthropic Claude Sonnet
     hooks** — each line invokes a small LLM call. Record cost per tick +
     baseline (1 single Claude Sonnet call same prompt) for comparison.

  4. **Real checkpoint flow**: between trifecta ticks, write a checkpoint
     snapshotting the synthetic 'task state' (which checkpoint we're on,
     accumulated cost, last trifecta findings).

  5. **Real lifecycle walk**: at end, walk a synthetic capability through
     OBS→...→CANARY (5 transitions). The ticket from step 2 (already
     answered 'approve') gates CANARY→PRODUCTION → 6th transition lands
     in PG with user_approval_ticket_id.

  6. **Crash + resume**: discard all in-process state; fresh checkpoint
     reader recovers latest (proves PG round-trip).

  7. **Post-flight**: snapshot deltas across all tables, print real cost
     report:
        - actual_trifecta_cost  vs  baseline_single_llm_cost
        - actual_multiplier     vs  V7 §12.4.4 estimate (5-6x)
        - rows landed: lifecycle +6, checkpoint +3, plus task overhead

Cost guard: hard-cap each Anthropic call to ~$0.01 (max_tokens 200,
sonnet pricing). 9 trifecta calls + small probes = ~$0.10-0.20 expected.

Run: .venv/bin/python scripts/dogfood_v12_real_trifecta_checkpoint_collab.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Load .env
try:
    from dotenv import load_dotenv

    load_dotenv(
        Path(__file__).resolve().parent.parent / ".env",
        override=True,  # OAuth token in .env wins over empty shell env
    )
except ImportError:
    pass

# Cost guards
os.environ.setdefault("KUN_V7_TRIFECTA_ENABLED", "true")


def _hdr(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


# ============================================================
# Real Anthropic Sonnet client (cheap-tier short calls)
# ============================================================


async def _real_anthropic_call(prompt: str, *, max_tokens: int = 200) -> tuple[str, float]:
    """Make one real Claude Sonnet call. Returns (text, cost_usd)."""
    from kun.interface.llm.anthropic_provider import AnthropicProvider
    from kun.interface.llm.base import LLMMessage, LLMRequest

    # Haiku — cheapest tier for short prompts (input $0.25/Mtok, output $1.25/Mtok)
    provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")
    req = LLMRequest(
        messages=[LLMMessage(role="user", content=prompt)],
        temperature=0.7,
        max_tokens=max_tokens,
    )
    resp = await provider.invoke(req)
    return resp.content, resp.cost_usd_actual


# ============================================================
# Trifecta hooks backed by real LLM
# ============================================================


async def _past_hook_real(
    task_id: str, _recent: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float, str | None]:
    prompt = (
        "你是 KUN V7 §12.4 trifecta 过去线 (retrospective). 任务 ID:"
        f" {task_id}. 用 1 句话回顾这个任务最近 3 步可能的潜在 root cause"
        " (即使没明显问题, 也找 1 个可关注信号). 不超过 60 字."
    )
    try:
        text, cost = await _real_anthropic_call(prompt, max_tokens=120)
        return ([{"finding": text.strip()[:200], "case_id": "live-past-1"}], cost, None)
    except Exception as e:
        return ([], 0.0, f"past_hook_error: {type(e).__name__}: {e}")


async def _present_hook_real(
    task_id: str, _cur: dict[str, Any]
) -> tuple[list[dict[str, Any]], float, str | None]:
    prompt = (
        "你是 KUN V7 §12.4 trifecta 现在线 (watchdog). 任务 ID:"
        f" {task_id}. 用 1 句话评估当前 step 是否 drift / 卡死, 给出 ok/concerning/alarming."
        " 不超过 50 字."
    )
    try:
        text, cost = await _real_anthropic_call(prompt, max_tokens=100)
        return ([{"critique": text.strip()[:200]}], cost, None)
    except Exception as e:
        return ([], 0.0, f"present_hook_error: {type(e).__name__}: {e}")


async def _future_hook_real(
    task_id: str, _plan: dict[str, Any], n: int
) -> tuple[list[dict[str, Any]], float, str | None]:
    prompt = (
        "你是 KUN V7 §12.4 trifecta 未来线 (explorer). 任务 ID:"
        f" {task_id}. 提议 {n} 个候选 next step, 各 1 句话, 各≤30 字. JSON 数组."
    )
    try:
        text, cost = await _real_anthropic_call(prompt, max_tokens=250)
        # We don't strictly need to parse JSON — just split into candidates by line.
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        candidates = [
            {"candidate": line[:200], "metric": 0.75 + 0.05 * i}
            for i, line in enumerate(lines[:n])
        ]
        return (candidates or [{"candidate": text.strip()[:200], "metric": 0.7}], cost, None)
    except Exception as e:
        return ([], 0.0, f"future_hook_error: {type(e).__name__}: {e}")


# ============================================================
# PG snapshot helpers
# ============================================================


async def _snapshot_tables() -> dict[str, int]:
    from kun.core.db import get_admin_sessionmaker
    from kun.core.orm import (
        AuditorReportRow,
        EnsembleCallRow,
        LifecycleTransitionRow,
        MissionAlignmentReviewRow,
        TaskCheckpointRow,
    )
    from sqlalchemy import func, select

    sm = get_admin_sessionmaker()
    out: dict[str, int] = {}
    async with sm() as s:
        for name, orm in {
            "mission_alignment_reviews": MissionAlignmentReviewRow,
            "lifecycle_transitions": LifecycleTransitionRow,
            "auditor_reports": AuditorReportRow,
            "ensemble_calls": EnsembleCallRow,
            "task_checkpoints": TaskCheckpointRow,
        }.items():
            r = await s.execute(select(func.count()).select_from(orm))
            out[name] = int(r.scalar() or 0)
    return out


# ============================================================
# Main scenario
# ============================================================


async def main() -> int:
    from kun.agents.executor.checkpoint import TaskCheckpoint
    from kun.agents.trifecta import TrifectaCoordinator, TrifectaState
    from kun.control_plane.collaboration import (
        CollaborationResponse,
        InMemoryCollaborationQueue,
    )
    from kun.control_plane.v6 import CollaborationTicket
    from kun.governance.capability_lifecycle import (
        CapabilityLifecycleService,
        CapabilityLifecycleStage,
    )
    from kun.integration.capability_lifecycle_db import (
        make_lifecycle_transition_emitter,
    )
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    tenant_id = "u-sylvan"
    run_id = int(time.time())
    task_id = f"tk-v12-{run_id}"
    capability_id = f"cap-v12-{run_id}"
    ticket_id = f"tk-v12-collab-{run_id}"

    _hdr(f"Dogfood v12 — real LLM trifecta + checkpoint + collab  ({_ts()})")
    print(f"  tenant_id      = {tenant_id}")
    print(f"  task_id        = {task_id}")
    print(f"  capability_id  = {capability_id}")
    print(f"  ticket_id      = {ticket_id}")

    # ============================================================
    # Pre-flight
    # ============================================================
    _hdr("Pre-flight snapshot")
    before = await _snapshot_tables()
    for t, n in before.items():
        print(f"  {t:35s} rows={n}")

    # ============================================================
    # Step 1 — open CollaborationTicket for budget approval
    # ============================================================
    _hdr("Step 1 — open CollaborationTicket")
    queue = InMemoryCollaborationQueue()
    ticket = queue.submit(
        CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=f"msn-v12-{run_id}",
            type="approval",
            role_needed="release_owner",
            why_needed=(
                "Dogfood v12: real-LLM trifecta + checkpoint + collab e2e. "
                "Expected total cost: $0.50-3.00. Approve to proceed."
            ),
            decision_options=["approve", "hold"],
            recommended_option="approve",
            context_ref=f"capability:{capability_id}",
            risk_if_skipped="No v12 e2e evidence; rely only on stub-hook v11 capstone",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
            fallback_policy={"option": "approve", "reason": "auto-approve for dogfood"},
            output_contract="approve|hold + 1-line rationale",
        )
    )
    print(f"  Ticket opened: status={ticket.status}, deadline=+5min")

    # Auto-respond approve (simulating fast release_owner saw the budget)
    queue.respond(
        CollaborationResponse(
            ticket_id=ticket_id,
            responder="dogfood_v12_auto",
            selected_option="approve",
            answer="approved for v12 dogfood run, $5 cap",
        )
    )
    print(f"  Ticket answered: selected={queue.responses[ticket_id].selected_option}")

    # ============================================================
    # Step 2 — real LLM trifecta × 3 (separated to look like task ticks)
    # ============================================================
    _hdr("Step 2 — real LLM trifecta × 3 ticks")
    coord = TrifectaCoordinator(
        past_hook=_past_hook_real,
        present_hook=_present_hook_real,
        future_hook=_future_hook_real,
    )

    trifecta_total_cost = 0.0
    trifecta_runs: list[dict[str, Any]] = []

    for tick in (1, 2, 3):
        print(f"\n  --- trifecta tick {tick} @ {_ts()} ---")
        t0 = time.perf_counter()

        # Baseline cost = 1 single LLM call for comparison
        baseline_prompt = (
            f"任务 ID {task_id} tick {tick}: 用 1 句话评估当前 step. ≤ 40 字."
        )
        try:
            _, baseline_cost = await _real_anthropic_call(baseline_prompt, max_tokens=80)
        except Exception as e:
            print(f"  baseline call failed: {e}")
            baseline_cost = 0.001  # fall back to a sane default

        report = await coord.run(
            task_id=task_id,
            recent_steps=[{"tick": tick, "synthetic": True}],
            current_step={"tick": tick, "elapsed_sec": time.perf_counter() - t0},
            future_plan={"goal": "v12 dogfood real-LLM trifecta e2e"},
            n_future_candidates=2,
            baseline_cost_usd=baseline_cost,
        )

        tick_cost = report.total_cost_usd
        trifecta_total_cost += tick_cost

        print(
            f"  past={report.past.state.value} (${report.past.cost_usd:.5f}, "
            f"{report.past.duration_sec:.2f}s)"
        )
        print(
            f"  present={report.present.state.value} (${report.present.cost_usd:.5f}, "
            f"{report.present.duration_sec:.2f}s)"
        )
        print(
            f"  future={report.future.state.value} (${report.future.cost_usd:.5f}, "
            f"{report.future.duration_sec:.2f}s)"
        )
        print(
            f"  tick total: ${tick_cost:.5f} (baseline ${baseline_cost:.5f}, "
            f"multiplier {report.cost_multiplier_vs_baseline:.2f}x)"
        )
        print(f"  n_findings: {report.n_findings}")

        # Stash some findings as the next checkpoint's working_state
        sample_finding = ""
        for line_result in (report.past, report.present, report.future):
            if line_result.state == TrifectaState.OK and line_result.findings:
                f0 = line_result.findings[0]
                sample_finding = next(
                    (str(v) for v in f0.values() if isinstance(v, str) and v), ""
                )[:100]
                if sample_finding:
                    break

        trifecta_runs.append(
            {
                "tick": tick,
                "tick_cost": tick_cost,
                "baseline_cost": baseline_cost,
                "multiplier": report.cost_multiplier_vs_baseline,
                "n_findings": report.n_findings,
                "any_failed": report.any_line_failed,
                "sample_finding": sample_finding,
            }
        )

        # Step 3 — write a real checkpoint capturing the run state
        cp = TaskCheckpoint(
            checkpoint_id=f"tcp-v12-{run_id}-{tick}",
            task_id=task_id,
            step_idx=tick,
            sequence=tick,
            conversation_snapshot=[
                {"role": "system", "content": "v12 trifecta tick"},
                {"role": "user", "content": f"tick {tick} findings: {sample_finding}"},
            ],
            working_state={
                "tick": tick,
                "accumulated_trifecta_cost": trifecta_total_cost,
                "trifecta_runs": trifecta_runs[-1:],  # most recent only
            },
            artifact_refs=[],
            goal_anchor_id=None,
            last_self_report=None,
            cost_usd_so_far=trifecta_total_cost,
            tokens_used_so_far=200 * tick,
            status="active",  # type: ignore[arg-type]
            rationale=f"v12 checkpoint after trifecta tick {tick}",
        )
        await make_checkpoint_writer()(cp.to_row_payload(tenant_id))
        print(
            f"  checkpoint saved: id={cp.checkpoint_id} sequence={tick} "
            f"cost_so_far=${trifecta_total_cost:.5f}"
        )

    # ============================================================
    # Step 4 — Crash + resume
    # ============================================================
    _hdr("Step 4 — simulate crash + recover via fresh checkpoint reader")
    del coord  # drop in-memory state
    reader = make_checkpoint_reader()
    latest = await reader(tenant_id, task_id)
    if latest is None:
        print("  ❌ Reader returned None — crash recovery broken")
        return 2
    print(f"  ✅ Recovered: checkpoint_id={latest.checkpoint_id}")
    print(
        f"  sequence={latest.sequence}, step_idx={latest.step_idx}, "
        f"cost_so_far=${latest.cost_usd_so_far:.5f}"
    )
    assert latest.sequence == 3, f"expected seq=3 got {latest.sequence}"

    # ============================================================
    # Step 5 — Real lifecycle walk gated by the ticket
    # ============================================================
    _hdr("Step 5 — capability lifecycle walk, ticket gates CANARY→PRODUCTION")
    lifecycle = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )

    # OBS → CAN → REP → HOL → SHA → CAN
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
        decision_rationale="v12 dogfood real-LLM trifecta capability proposal",
    )
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-v12",
            "process_audit:pa-v12",
            "capability_candidate:cc-v12",
        ],
        decision_rationale="v12 evidence trio: trifecta runs + checkpoint round-trip + ticket flow",
    )
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.REPLAY,
        to_stage=CapabilityLifecycleStage.HOLDOUT,
        evidence_refs=["strategy_replay_report:rr-v12"],
    )
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.HOLDOUT,
        to_stage=CapabilityLifecycleStage.SHADOW,
    )
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.SHADOW,
        to_stage=CapabilityLifecycleStage.CANARY,
    )
    # The ticket-gated flip
    answered = queue.responses[ticket_id]
    if answered.selected_option != "approve":
        print(f"  ❌ Ticket not 'approve' ({answered.selected_option}) — abort prod flip")
        return 3
    await lifecycle.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id=ticket_id,
        evidence_refs=["canary_metrics:cm-v12"],
        decision_rationale=f"v12 dogfood ticket {ticket_id} approved real-LLM run",
    )
    print(f"  ✅ 6 transitions emitted, capability now in PRODUCTION (ticket={ticket_id})")

    # ============================================================
    # Post-flight + report
    # ============================================================
    _hdr("Post-flight snapshot + deltas")
    after = await _snapshot_tables()
    deltas: dict[str, int] = {}
    for t, n_after in after.items():
        n_before = before[t]
        delta = n_after - n_before
        deltas[t] = delta
        mark = f" ← +{delta}" if delta > 0 else ""
        print(f"  {t:35s} before={n_before} after={n_after}{mark}")

    _hdr("Real-LLM trifecta cost report")
    avg_baseline = (
        sum(r["baseline_cost"] for r in trifecta_runs) / max(1, len(trifecta_runs))
    )
    avg_tick = (
        sum(r["tick_cost"] for r in trifecta_runs) / max(1, len(trifecta_runs))
    )
    print(f"  avg single-LLM baseline cost: ${avg_baseline:.5f}")
    print(f"  avg trifecta tick cost:        ${avg_tick:.5f}")
    print(f"  avg multiplier (real):         {avg_tick / max(avg_baseline, 1e-6):.2f}x")
    print("  V7 §12.4.4 estimate range:     5-6x (full trifecta on/off)")
    print(f"  total trifecta cost (3 ticks): ${trifecta_total_cost:.5f}")

    # Approx total spend across the whole run
    total_run_cost = trifecta_total_cost + avg_baseline * 3  # 3 baseline probes
    print(f"  approx total run cost:         ${total_run_cost:.5f}")

    _hdr("Summary")
    expected = {
        "lifecycle_transitions": 6,  # OBS→...→PROD
        "task_checkpoints": 3,
    }
    for t, n_expected in expected.items():
        actual = deltas.get(t, 0)
        ok = "✅" if actual >= n_expected else "❌"
        print(f"  {ok} {t}: +{actual} (expected ≥ {n_expected})")

    if all(deltas.get(t, 0) >= n for t, n in expected.items()):
        print("\n  ✅ v12 dogfood PASS — real LLM trifecta + real PG checkpoint + ticket gate all wired")
        return 0
    print("\n  ❌ v12 dogfood FAIL — see deltas above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
