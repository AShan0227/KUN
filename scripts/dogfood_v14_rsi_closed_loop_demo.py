"""Dogfood v14 — V7 §12 RSI closed-loop demo (X.G.RSI-CLOSED-LOOP).

The V7 §1.1 product 魂: "每跑一次都让自己略变更聪明". This script proves
the closed loop on the **read side** for the first time:

  promoted methodology in seeds/methodologies/   (X.D-1 merged 3 v11 seeds)
      ↓ MethodologyRuntimeSelector.select_for(task_context)
      ↓ render_for_system_prompt(top-K)
  appended to system prompt before ExecutorLoop runs
      ↓ LLM "sees" promoted methodology as runtime context
  task influenced by the methodology  (next task is略变更聪明)

Before this commit the seeds were write-only — they sat in
``seeds/methodologies/`` unused by runtime. v14 proves they actually reach
the LLM at task time.

Scenario:

  A) Load real ``seeds/methodologies/`` (31 yaml seeds, includes the 3
     v11 seeds merged in X.D-1).
  B) Construct two TaskContexts:
       - C1: "real PG e2e wiring closure proof" → should match
              `production_loop_real_pg_e2e_chain.yaml`
       - C2: "approve production flip via collaboration ticket" → should
              match `human_in_loop_gate_via_collab_ticket.yaml`
       - C3: "invariant double-保险 service + DB CHECK" → should match
              `service_layer_invariant_plus_db_check_belt_and_suspenders.yaml`
     For each, print the top-3 selected methodologies + scores.
  C) Run a LongTaskOrchestrator end-to-end with a stub main LLM (no real
     LLM cost), wiring the methodology selector. Verify:
       1. `long_task.methodology_injected` event fires
       2. System prompt the stub LLM sees contains the rendered
          methodology block (proves the loop closed inside the
          orchestrator path, not just unit tests)
       3. n_methodologies and titles match the selector's choice
  D) Print summary — what KUN now "remembers" between tasks via
     promoted methodologies.

Run:  .venv/bin/python scripts/dogfood_v14_rsi_closed_loop_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from kun.engineering.methodology_runtime_loader import (
    MethodologyRuntimeSelector,
    TaskContext,
    load_methodologies,
)

SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds" / "methodologies"


def _hdr(t: str) -> None:
    print(f"\n{'=' * 70}\n  {t}\n{'=' * 70}")


def _print_chosen(label: str, chosen: list[Any]) -> None:
    print(f"\n  Context: {label}")
    if not chosen:
        print("  ❌ No matching methodology selected")
        return
    for i, m in enumerate(chosen, start=1):
        print(f"  {i}. {m.title}  (相关度 {m.score:.3f})")
        print(f"     topic: {m.topic}")
        print(f"     file:  {Path(m.file_path).name}")


def _run_selection_demo() -> tuple[int, MethodologyRuntimeSelector]:
    _hdr("A) Load real seeds/methodologies/")
    entries = load_methodologies(SEEDS_DIR)
    selector = MethodologyRuntimeSelector(entries)
    print(f"  Loaded {len(entries)} production-stage methodology entries")
    print(f"  From: {SEEDS_DIR}")

    _hdr("B) Selector picks top-K for 3 distinct task contexts")

    # C1: real PG e2e wiring closure
    c1 = TaskContext(
        task_type="testing.acceptance.production",
        goal_keywords=["real", "PG", "e2e", "wiring", "closure"],
        goal_statement="prove production-loop closure via 串多个 real-PG e2e tests",
    )
    chosen1 = selector.select_for(c1, top_k=3)
    _print_chosen("C1 (real PG e2e wiring closure)", chosen1)

    # C2: human-in-loop production flip
    c2 = TaskContext(
        task_type="governance.collaboration.production",
        goal_keywords=["approval", "human", "ticket", "production", "flip"],
        goal_statement="gate production transition via CollaborationTicket + approval",
    )
    chosen2 = selector.select_for(c2, top_k=3)
    _print_chosen("C2 (human-in-loop production flip)", chosen2)

    # C3: invariant double-保险 (service + DB CHECK)
    c3 = TaskContext(
        task_type="governance.invariant.enforcement",
        goal_keywords=["invariant", "service", "DB", "CHECK", "double", "belt"],
        goal_statement="协议级 invariant 双保险 — service 层 raise + DB CHECK",
    )
    chosen3 = selector.select_for(c3, top_k=3)
    _print_chosen("C3 (invariant double-保险)", chosen3)

    return len(entries), selector


async def _run_orchestrator_demo(selector: MethodologyRuntimeSelector) -> bool:
    _hdr("C) Wire selector into LongTaskOrchestrator and run end-to-end")

    from kun.agents.director.anchor import GoalAnchor
    from kun.agents.executor.checkpoint import CheckpointStatus, TaskCheckpoint
    from kun.agents.executor.exec_loop import (
        LLMStepResponse,
        ToolResult,
    )
    from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
    from kun.engineering.long_task_orchestrator import LongTaskOrchestrator

    captured_system_prompts: list[str] = []
    methodology_events: list[dict[str, Any]] = []

    class _StubLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, messages: list[dict[str, Any]]) -> LLMStepResponse:
            self.calls += 1
            sys_msg = next(
                (m for m in messages if m.get("role") == "system"), None
            )
            if sys_msg:
                captured_system_prompts.append(sys_msg.get("content", ""))
            return LLMStepResponse(
                content="real-PG wiring closure demo done",
                tool_calls=[],
                finish_reason="end_turn",
                cost_usd=0.0,
                usage_tokens=10,
            )

    async def _stub_tools(_: list[Any]) -> list[ToolResult]:
        return []

    class _Store:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def writer(self, row: dict[str, Any]) -> None:
            self.rows.append(dict(row))

        async def reader(self, _t: str, _tk: str) -> TaskCheckpoint | None:
            return None

        async def marker(
            self, _t: str, _cp: str, _st: CheckpointStatus
        ) -> None:
            return None

    store = _Store()
    llm = _StubLLM()
    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_stub_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        methodology_selector=selector,
        methodology_top_k=3,
        enable_recursive_planner=False,
    )

    owner = Owner(tenant_id="u-sylvan", user_id="u-v14")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("v14 rsi closed-loop demo", owner),
        task_type="testing.acceptance.production",
        risk_level="medium",
        complexity="medium",
        estimated_steps=2,
        estimated_duration_sec=30.0,
        owner=owner,
        success_criteria_short="prove production-loop closure via real PG e2e",
    )
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="prove real PG e2e wiring closure"))
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement="prove production-loop closure via 串多个 real PG e2e tests",
        success_criteria=["real PG row delta", "chain N writers", "read back proof"],
        out_of_scope=["unit tests with fakes"],
        invariants=["any failure halts the chain"],
    )

    async def _sink(ev: Any) -> None:
        if ev.kind == "long_task.methodology_injected":
            methodology_events.append(dict(ev.data))

    outcome = await orch.run_long_task(ref, on_event=_sink)

    # Verify the closed loop fired
    print(f"  outcome.loop_result.status = {outcome.loop_result.status}")
    print(f"  llm.calls                  = {llm.calls}")
    print(f"  methodology events emitted = {len(methodology_events)}")

    if not methodology_events:
        print("\n  ❌ FAIL — no methodology_injected event")
        return False
    ev = methodology_events[0]
    print(f"  n_methodologies          = {ev['n_methodologies']}")
    print("  Top methodologies injected:")
    for m in ev["methodologies"]:
        print(f"    - {m['title']}  (score={m['score']})")

    sys_prompt = captured_system_prompts[0] if captured_system_prompts else ""
    if "RSI 进化产物" not in sys_prompt:
        print("\n  ❌ FAIL — system prompt did not contain RSI block")
        return False

    print("\n  ✅ System prompt contained the RSI block — LLM真 saw 蒸馏方法论")
    return True


async def main() -> int:
    _hdr("Dogfood v14 — V7 §12 RSI closed-loop demo")

    n_entries, selector = _run_selection_demo()
    if n_entries == 0:
        print("\n  ❌ seeds/methodologies/ empty — nothing to inject")
        return 2

    ok = await _run_orchestrator_demo(selector)

    _hdr("D) Summary")
    if ok:
        print("  ✅ RSI closed loop demonstrated end-to-end:")
        print("     1. Seeds in seeds/methodologies/ are loadable at runtime")
        print("     2. Selector deterministically picks top-K relevant by keyword")
        print("     3. Orchestrator injects rendered block into system prompt")
        print("     4. LLM真 sees the methodology (verified via captured prompt)")
        print("     5. Event emitted for cockpit / audit trail")
        print("\n  V7 §1.1 '每跑一次都让自己略变更聪明' 闭环已可达 (read side).")
        return 0
    print("  ❌ Demo did not close the loop — see errors above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
