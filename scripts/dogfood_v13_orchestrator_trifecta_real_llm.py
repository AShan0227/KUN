"""Dogfood v13 — real orchestrator path firing trifecta with real LLM hooks.

⚠️ FIXTURE/DEMO (audit F059): PASS only checks TrifectaState==OK (hooks ran), not output quality; not run in CI. A green run does NOT prove real RSI closure. See docs/audit/proposals/demo-script-honesty.md.

V7 Phase X.E.TRIFECTA-WIRING evidence: previously TrifectaCoordinator was
orphan; v12 proved it works in isolation with real Haiku; **v13 proves it
fires from inside LongTaskOrchestrator.run_long_task** when wired.

Setup:
  - Stub main-line LLM (returns 3 fake tool steps + 1 final) — task itself
    doesn't burn real LLM, only the trifecta hooks do
  - TrifectaCoordinator with **real Anthropic Haiku** past/present/future
    hooks (same as v12)
  - LongTaskOrchestrator constructed with `trifecta_coordinator` +
    `trifecta_every_n_steps=2`
  - Run a synthetic long task

Expected:
  - 4 main-line LLM calls (3 tool + 1 final)
  - Trifecta fires at call 2 + call 4 → 2 real LLM trifecta ticks
  - Each tick emits long_task.trifecta_tick event with per-line state + cost
  - Real Haiku cost ≈ $0.001 total

Run: .venv/bin/python scripts/dogfood_v13_orchestrator_trifecta_real_llm.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Load .env with override (OAuth token from .env wins over empty shell env)
try:
    from dotenv import load_dotenv

    load_dotenv(
        Path(__file__).resolve().parent.parent / ".env",
        override=True,
    )
except ImportError:
    pass

os.environ.setdefault("KUN_V7_TRIFECTA_ENABLED", "true")


def _hdr(t: str) -> None:
    print(f"\n{'=' * 70}\n  {t}\n{'=' * 70}")


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


# ============================================================
# Real Haiku helper (same as v12)
# ============================================================


async def _real_haiku(prompt: str, *, max_tokens: int = 120) -> tuple[str, float]:
    from kun.interface.llm.anthropic_provider import AnthropicProvider
    from kun.interface.llm.base import LLMMessage, LLMRequest

    provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")
    req = LLMRequest(
        messages=[LLMMessage(role="user", content=prompt)],
        temperature=0.7,
        max_tokens=max_tokens,
    )
    resp = await provider.invoke(req)
    return resp.content, resp.cost_usd_actual


# ============================================================
# Real-LLM trifecta hooks
# ============================================================


async def _past(
    task_id: str, _recent: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float, str | None]:
    text, cost = await _real_haiku(
        f"V7 trifecta past line. task={task_id}. 1 句话回顾, ≤40 字."
    )
    return ([{"finding": text.strip()[:200], "case_id": "v13-past"}], cost, None)


async def _present(
    task_id: str, _cur: dict[str, Any]
) -> tuple[list[dict[str, Any]], float, str | None]:
    text, cost = await _real_haiku(
        f"V7 trifecta present line. task={task_id}. 1 句话评估 drift, ≤40 字."
    )
    return ([{"critique": text.strip()[:200]}], cost, None)


async def _future(
    task_id: str, _plan: dict[str, Any], n: int
) -> tuple[list[dict[str, Any]], float, str | None]:
    text, cost = await _real_haiku(
        f"V7 trifecta future line. task={task_id}. 提议 {n} 个候选, 每个≤20 字."
    )
    lines = [line.strip() for line in text.split("\n") if line.strip()][:n]
    return (
        [{"candidate": line[:120], "metric": 0.75 + 0.05 * i} for i, line in enumerate(lines)]
        or [{"candidate": text.strip()[:120], "metric": 0.7}],
        cost,
        None,
    )


# ============================================================
# Stub main-line LLM + tool executor (cheap, deterministic)
# ============================================================


def _stub_main_llm_responses() -> list[Any]:
    from kun.agents.executor.exec_loop import LLMStepResponse, ToolCall

    # 3 tool steps + 1 final = 4 LLM calls total
    return [
        LLMStepResponse(
            content="thinking step 1...",
            tool_calls=[ToolCall(tool_id="t-1", name="search", arguments={"q": "a"})],
            finish_reason="tool_use",
            cost_usd=0.0,  # stub — no real cost on main line
            usage_tokens=10,
        ),
        LLMStepResponse(
            content="thinking step 2...",
            tool_calls=[ToolCall(tool_id="t-2", name="search", arguments={"q": "b"})],
            finish_reason="tool_use",
            cost_usd=0.0,
            usage_tokens=10,
        ),
        LLMStepResponse(
            content="thinking step 3...",
            tool_calls=[ToolCall(tool_id="t-3", name="search", arguments={"q": "c"})],
            finish_reason="tool_use",
            cost_usd=0.0,
            usage_tokens=10,
        ),
        LLMStepResponse(
            content="final answer for v13 dogfood",
            tool_calls=[],
            finish_reason="end_turn",
            cost_usd=0.0,
            usage_tokens=10,
        ),
    ]


class _StubMainLLM:
    def __init__(self) -> None:
        self.responses = _stub_main_llm_responses()
        self.calls = 0

    async def __call__(self, _messages: list[dict[str, Any]]) -> Any:
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r


async def _passthrough_tools(calls):
    from kun.agents.executor.exec_loop import ToolResult

    return [ToolResult(tool_id=c.tool_id, content="ok") for c in calls]


# ============================================================
# Build TaskRef with anchor (same shape as orchestrator unit tests)
# ============================================================


def _build_ref():
    from kun.agents.director.anchor import GoalAnchor
    from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec

    owner = Owner(tenant_id="u-sylvan", user_id="u-v13")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("v13 dogfood task", owner),
        task_type="coding.python.fastapi",
        risk_level="medium",
        complexity="medium",
        estimated_steps=4,
        estimated_duration_sec=60.0,
        owner=owner,
        success_criteria_short="v13 dogfood completes",
    )
    spec = TaskSpec(
        goal_detail="v13 dogfood — exercise trifecta wiring from orchestrator",
        success_metrics=["trifecta fires from orchestrator", "no main-task crash"],
        subtasks_hint=[],
    )
    ref = TaskRef(meta=meta, spec=spec)
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement="ship trifecta orchestrator wiring evidence",
        success_criteria=["trifecta fires at every N=2 main-line steps with real LLM"],
        out_of_scope=["full long-task LLM run"],
        invariants=["main task must complete even if trifecta line raises"],
    )
    return ref


# ============================================================
# Main
# ============================================================


async def main() -> int:
    from kun.agents.trifecta import TrifectaCoordinator
    from kun.engineering.long_task_orchestrator import LongTaskOrchestrator

    _hdr(f"Dogfood v13 — orchestrator-fired real-LLM trifecta  ({_ts()})")

    # Build coordinator with real Haiku hooks
    coord = TrifectaCoordinator(
        past_hook=_past,
        present_hook=_present,
        future_hook=_future,
    )

    # In-memory checkpoint store (we don't care about persistence here —
    # v12 already proved real PG checkpoint resume works)
    class _Store:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def writer(self, row: dict[str, Any]) -> None:
            self.rows.append(dict(row))

        async def reader(self, _t: str, _tk: str) -> Any:
            return None

        async def marker(self, _t: str, _cp: str, _st: Any) -> None:
            return None

    store = _Store()
    main_llm = _StubMainLLM()

    orch = LongTaskOrchestrator(
        llm_invoker=main_llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=2,
        trifecta_n_future_candidates=2,
        max_steps=10,
        enable_recursive_planner=False,
    )

    events: list[dict[str, Any]] = []

    async def _sink(ev: Any) -> None:
        events.append({"kind": ev.kind, "data": dict(ev.data)})
        # Print interesting events inline
        if "trifecta" in ev.kind:
            print(f"  📡 {ev.kind}")
            for k, v in ev.data.items():
                print(f"      {k}: {v}")

    _hdr("Running orchestrator with trifecta wired")
    ref = _build_ref()
    outcome = await orch.run_long_task(ref, on_event=_sink)
    print(f"\n  outcome.loop_result.status = {outcome.loop_result.status}")
    print(f"  events emitted             = {outcome.events_emitted}")
    print(f"  main-line LLM calls        = {main_llm.calls}")

    # ============================================================
    # Verify
    # ============================================================
    _hdr("Verify trifecta ticks fired from orchestrator")
    ticks = [e for e in events if e["kind"] == "long_task.trifecta_tick"]
    print(f"  Trifecta ticks: {len(ticks)}")
    for i, ev in enumerate(ticks):
        d = ev["data"]
        print(
            f"  tick {i + 1}: call_count={d['call_count']} "
            f"past={d['past_state']} present={d['present_state']} "
            f"future={d['future_state']} n_findings={d['n_findings']} "
            f"cost=${d['total_cost_usd']:.5f} multiplier={d['cost_multiplier_vs_baseline']}"
        )

    ok = (
        outcome.loop_result.status == "final"
        and len(ticks) == 2
        and all(d["data"]["past_state"] == "ok" for d in ticks)
        and all(d["data"]["present_state"] == "ok" for d in ticks)
        and all(d["data"]["future_state"] == "ok" for d in ticks)
    )

    _hdr("Summary")
    if ok:
        total_cost = sum(t["data"]["total_cost_usd"] for t in ticks)
        print("  ✅ v13 dogfood PASS")
        print(f"  ✅ Trifecta fired {len(ticks)}x from orchestrator step counter")
        print("  ✅ All 3 lines OK with real Haiku across both ticks")
        print(f"  ✅ Total real-LLM trifecta cost: ${total_cost:.5f}")
        return 0
    print("  ❌ v13 dogfood FAIL")
    print(f"  outcome.status = {outcome.loop_result.status}")
    print(f"  trifecta tick count = {len(ticks)} (expected 2)")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
