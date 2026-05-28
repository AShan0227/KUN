"""V7 Phase X.B end-to-end smoke harness.

# SCOPE (V7 §16.3): smoke validator, not a real dogfood. 用 stub provider +
# in-memory fake session, 不打 PG, 不打真 LLM, 不入 capability_card. 目的: 把
# 本 session 落地的 X.B 6 块代码各调一次, 真在 runtime 跑一遍, 防 "代码写完
# 但路径不通" 的反模式 (V7 §16.2 反模式 1).

跑法:
    .venv/bin/python scripts/v7_xb_smoke.py

预期输出 (stdout):
    ═══ V7 Phase X.B Smoke Harness ═══
    [1/6] ✅ Mission Director DB writer (X.B.MD)
    [2/6] ✅ Capability Lifecycle DB writer (X.B.LC)
    [3/6] ✅ Auditor Reports DB writer (X.B.AR)
    [4/6] ✅ Cockpit API readers (X.B.UI)
    [5/6] ✅ Mission Director runner tick (X.B.MDR)
    [6/6] ✅ Ensemble invoker + DB log (X.B.ENS)
    ───
    全部 6 块 X.B 真在 runtime 走通. 软件层完成度 100%.

退出码:
    0 — all 6 X.B 模块 smoke pass
    1 — 任何 X.B 模块 smoke fail (打印 traceback)

Phase X.B+ 真 dogfood v9 (跑长任务真验证 trifecta + ensemble) 见
docs/dist-output/dogfood-v9-task-plan.md.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar

# ============================================================
# Fake session_scope used across all 6 smoke segments
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
        return None


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _ReadFakeSession:
    """Session that supports both add() (write) AND execute() (read).

    For read, filters captured rows by the table being queried so each
    reader only sees rows of the right type (real PG does this via FROM).
    """

    # Map ORM Row class name → table name (lower) — keeps the smoke
    # decoupled from importing every ORM class at the top.
    _TYPE_TO_TABLE: ClassVar[dict[str, str]] = {
        "MissionAlignmentReviewRow": "mission_alignment_reviews",
        "PlanChangeProposalRow": "plan_change_proposals",
        "LifecycleTransitionRow": "lifecycle_transitions",
        "AuditorReportRow": "auditor_reports",
        "EnsembleCallRow": "ensemble_calls",
    }

    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> _FakeResult:
        # Inspect the SQLAlchemy select's target table from the stmt object;
        # if we can extract it, filter rows by class name. Otherwise return all.
        target_table: str | None = None
        try:
            # SQLAlchemy 2.x select stores column entities; use .froms or
            # ._raw_columns / inspect.compile() — easiest is str(stmt).
            sql = str(stmt).lower()
            for typename, tablename in self._TYPE_TO_TABLE.items():
                if tablename in sql:
                    target_table = typename
                    break
        except Exception:  # pragma: no cover - defensive
            target_table = None

        if target_table is None:
            return _FakeResult(list(self._sink))
        filtered = [r for r in self._sink if type(r).__name__ == target_table]
        return _FakeResult(filtered)


def _install_writable_session(captured: list[Any]) -> None:
    """Install a fake session_scope into kun.core.db (both read + write)."""
    import kun.core.db as db_mod

    @asynccontextmanager
    async def fake_scope(**_: Any) -> AsyncIterator[_ReadFakeSession]:
        yield _ReadFakeSession(captured)

    db_mod.session_scope = fake_scope  # type: ignore[assignment]


# ============================================================
# Segment 1 — X.B.MD: Mission Director DB writer
# ============================================================


async def smoke_mission_director_db(captured: list[Any]) -> None:
    from kun.agents.mission_director.service import (
        AlignmentVerdict,
        MissionAlignmentReview,
        MissionDirectorService,
    )
    from kun.integration.mission_director_db import (
        make_mission_review_emitter,
    )

    service = MissionDirectorService(
        review_emitter=make_mission_review_emitter("tenant-smoke"),
    )
    review = await service.review_mission(
        task_id="tk-smoke-md",
        task_plan_version="v1",
        info_gap_coverage=0.9,
        decomposition_coverage=0.8,
        evidence_coverage=0.7,
    )
    assert review.verdict == AlignmentVerdict.OK, (
        f"expected OK verdict, got {review.verdict}"
    )
    assert isinstance(review, MissionAlignmentReview)
    # At least 1 row was added
    md_rows = [r for r in captured if type(r).__name__ == "MissionAlignmentReviewRow"]
    assert md_rows, "MissionAlignmentReviewRow was not added to fake session"


# ============================================================
# Segment 2 — X.B.LC: Capability Lifecycle DB writer
# ============================================================


async def smoke_capability_lifecycle_db(captured: list[Any]) -> None:
    from kun.governance.capability_lifecycle import (
        CapabilityLifecycleService,
        CapabilityLifecycleStage,
    )
    from kun.integration.capability_lifecycle_db import (
        make_lifecycle_transition_emitter,
    )

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter("tenant-smoke"),
    )
    record = await service.transition(
        capability_id="cap-smoke-1",
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-smoke",
            "process_audit:pa-smoke",
            "capability_candidate:cc-smoke",
        ],
        decision_rationale="smoke harness",
    )
    assert record.to_stage == CapabilityLifecycleStage.REPLAY
    lct_rows = [r for r in captured if type(r).__name__ == "LifecycleTransitionRow"]
    assert lct_rows, "LifecycleTransitionRow was not added"


# ============================================================
# Segment 3 — X.B.AR: Auditor Reports DB writer
# ============================================================


async def smoke_auditor_report_db(captured: list[Any]) -> None:
    from kun.integration.auditor_report_db import (
        AuditorReport,
        make_auditor_report_emitter,
    )

    emit = make_auditor_report_emitter("tenant-smoke")
    report = AuditorReport(
        report_id="ar-smoke-1",
        audited_capability="ensemble invoker",
        audited_at=datetime.now(UTC),
        auditor_provider="anthropic/claude-opus",
        design_promise="multi-LLM ensemble 真在 runtime path 跑",
        real_code_path="kun/integration/ensemble_invoker.py",
        bypass_methods=[],
        risk_level="P2",
        allow_release=True,
        rationale="smoke pass — invoker drop-in for LongTaskOrchestrator",
    )
    await emit(report)
    ar_rows = [r for r in captured if type(r).__name__ == "AuditorReportRow"]
    assert ar_rows, "AuditorReportRow was not added"


# ============================================================
# Segment 4 — X.B.UI: Cockpit API readers
# ============================================================


async def smoke_cockpit_readers(_captured: list[Any]) -> None:
    from kun.api.cockpit_readers import (
        list_recent_auditor_reports,
        list_recent_lifecycle_transitions,
        list_recent_mission_reviews,
    )

    # All 3 readers callable with no errors (rows captured in fake session
    # carry through via .execute → ._rows)
    mr = await list_recent_mission_reviews(tenant_id="tenant-smoke")
    lt = await list_recent_lifecycle_transitions(tenant_id="tenant-smoke")
    ar = await list_recent_auditor_reports(tenant_id="tenant-smoke")
    # Each reader returns a list (may be empty in smoke if previous segments
    # used different row types — that's fine, the smoke only verifies path)
    assert isinstance(mr, list)
    assert isinstance(lt, list)
    assert isinstance(ar, list)


# ============================================================
# Segment 5 — X.B.MDR: Mission Director runner tick
# ============================================================


async def smoke_mission_director_runner(captured: list[Any]) -> None:
    from kun.agents.mission_director.runner import (
        MissionCoverageInputs,
        build_default_runner,
    )

    async def _coverage(task_id: str) -> MissionCoverageInputs | None:
        return MissionCoverageInputs(
            task_plan_version="v1",
            info_gap_coverage=0.85,
            decomposition_coverage=0.75,
            evidence_coverage=0.6,
            observed_findings=[f"smoke finding for {task_id}"],
        )

    async def _active_tasks() -> list[str]:
        return ["tk-smoke-mdr-1", "tk-smoke-mdr-2"]

    runner = build_default_runner(
        tenant_id="tenant-smoke",
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()
    assert len(reviews) == 2, f"expected 2 reviews, got {len(reviews)}"
    assert runner.stats["review_count"] == 2
    md_rows_after = [
        r for r in captured if type(r).__name__ == "MissionAlignmentReviewRow"
    ]
    # Segment 1 already wrote 1; runner tick wrote 2 more → total ≥ 3
    assert len(md_rows_after) >= 3, (
        f"MissionAlignmentReviewRow count ≥ 3 expected, got {len(md_rows_after)}"
    )


# ============================================================
# Segment 6 — X.B.ENS: Ensemble invoker + DB log
# ============================================================


async def smoke_ensemble_invoker(captured: list[Any]) -> None:
    from kun.integration.ensemble_invoker import (
        make_ensemble_call_log_emitter,
        make_ensemble_llm_invoker,
    )
    from kun.interface.llm.stub_provider import StubProvider

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2],
        purpose="execution",
        call_log_emitter=make_ensemble_call_log_emitter("tenant-smoke"),
    )
    step = await invoker(
        [
            {"role": "system", "content": "你是 KUN smoke test"},
            {"role": "user", "content": "say hi"},
        ]
    )
    assert step.content, "step.content empty after ensemble invoke"
    ec_rows = [r for r in captured if type(r).__name__ == "EnsembleCallRow"]
    assert ec_rows, "EnsembleCallRow was not added"
    assert ec_rows[0].n_providers_total == 2
    assert ec_rows[0].failure_count == 0


# ============================================================
# Main runner
# ============================================================


SEGMENTS = [
    ("X.B.MD  Mission Director DB writer", smoke_mission_director_db),
    ("X.B.LC  Capability Lifecycle DB writer", smoke_capability_lifecycle_db),
    ("X.B.AR  Auditor Reports DB writer", smoke_auditor_report_db),
    ("X.B.UI  Cockpit API readers", smoke_cockpit_readers),
    ("X.B.MDR Mission Director runner tick", smoke_mission_director_runner),
    ("X.B.ENS Ensemble invoker + DB log", smoke_ensemble_invoker),
]


async def main() -> int:
    print("=" * 60)
    print("    V7 Phase X.B Smoke Harness")
    print("    (in-memory stub providers + fake session — no PG / no LLM)")
    print("=" * 60)

    captured: list[Any] = []
    _install_writable_session(captured)

    failures: list[tuple[str, str]] = []
    for i, (label, smoke_fn) in enumerate(SEGMENTS, start=1):
        try:
            await smoke_fn(captured)
            print(f"  [{i}/6] PASS  {label}")
        except Exception as e:
            print(f"  [{i}/6] FAIL  {label}")
            tb = traceback.format_exc()
            print(f"         {type(e).__name__}: {e}")
            failures.append((label, tb))

    print("-" * 60)
    if not failures:
        print(
            "  All 6 X.B segments smoke-passed. "
            "Software layer V7 Phase X.B = 100%."
        )
        print(f"  Captured rows: {len(captured)}")
        rows_by_type: dict[str, int] = {}
        for r in captured:
            tn = type(r).__name__
            rows_by_type[tn] = rows_by_type.get(tn, 0) + 1
        for tn, n in sorted(rows_by_type.items()):
            print(f"    - {tn}: {n}")
        print("=" * 60)
        return 0

    print(f"  ❌ {len(failures)} segments FAILED")
    for label, tb in failures:
        print("\n--- failure: " + label)
        print(tb)
    print("=" * 60)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
