"""V7 Phase X.I — production-path traceability primitive + 4 consumers.

End-to-end coverage of the X.I wave: a single primitive
(``check_symbol_reachable``) used by:

  - X.I-1 Auditor Angle 8 (heuristic auditor + LLM auditor render)
  - X.I-2 Gate R6 rule (rejects unreachable target_module)
  - X.I-3 TaskSpec.production_entry_changes_required field
  - X.I-4 Trifecta past hook → bug_root_cause_cases

The tests live as integration tests because they exercise the real
AST scanner over kun/engineering/orchestrator.py + friends.
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.governance.production_path_traceability import (
    ReachabilityResult,
    check_symbol_reachable,
)

pytestmark = pytest.mark.integration

# ============================================================
# X.I core: production-path-traceability primitive
# ============================================================


def test_known_production_class_is_reachable() -> None:
    """LongTaskOrchestrator IS instantiated in orchestrator.py."""
    r = check_symbol_reachable("LongTaskOrchestrator")
    assert isinstance(r, ReachabilityResult)
    assert r.reachable is True
    assert any("orchestrator.py" in e for e in r.entries_hit)


def test_obvious_orphan_is_not_reachable() -> None:
    """Made-up name → no production entry references it."""
    r = check_symbol_reachable("ThisSymbolDoesNotExistAnywhere1234")
    assert r.reachable is False
    assert r.is_orphan is True


def test_idle_batch_step_is_reachable() -> None:
    """MethodologyDistillStep is invoked by idle_batch_worker (production)."""
    r = check_symbol_reachable("MethodologyDistillStep")
    assert r.reachable is True


def test_empty_symbol_is_not_reachable() -> None:
    r = check_symbol_reachable("")
    assert r.reachable is False


# ============================================================
# X.I-2: Gate R6 production reachability check
# ============================================================


def test_gate_check_reachability_passes_when_module_known() -> None:
    """Real production module — Gate's R6 check returns ok=True."""
    from kun.agents.gate.service import _check_production_path_reachability

    # With kind=runtime + reachable symbol → pass
    ok, _ = _check_production_path_reachability(
        {"target_module": "LongTaskOrchestrator", "kind": "runtime"}
    )
    assert ok is True


def test_gate_check_reachability_fails_when_module_orphan() -> None:
    """Made-up module — Gate's R6 returns ok=False."""
    from kun.agents.gate.service import _check_production_path_reachability

    ok, reason_str = _check_production_path_reachability(
        {"target_module": "OrphanedXYZWidget1234", "kind": "runtime"}
    )
    assert ok is False
    assert "production_path_unreachable" in reason_str


def test_gate_check_reachability_passes_methodology_kind() -> None:
    """Methodology kind skips the reachability check (consumer is the
    runtime selector, not a code path)."""
    from kun.agents.gate.service import _check_production_path_reachability

    ok, reason_str = _check_production_path_reachability(
        {"kind": "methodology", "target_module": "OrphanedXYZWidget"}
    )
    assert ok is True
    assert "methodology" in reason_str


def test_gate_check_reachability_no_target_module_passes() -> None:
    """Empty target_module → check passes (no info to fail on)."""
    from kun.agents.gate.service import _check_production_path_reachability

    ok, _ = _check_production_path_reachability({})
    assert ok is True


@pytest.mark.asyncio
async def test_gate_admit_rejects_unreachable_target_module() -> None:
    """End-to-end: admit returns 'reject' when R6 fails."""
    from kun.agents.gate.service import GateService

    gate = GateService()
    decision = await gate.admit(
        {
            "experiment_id": "exp-orphan",
            "target_module": "OrphanedXYZWidget1234",
            "kind": "runtime",
        },
        test_report={"pass_rate": 1.0, "trials": 10, "fingerprint": "f"},
        diagnostic_record={"kind": "fix", "rationale": "..."},
        debrief={"evidence_quality_score": 0.9, "verdict": "pass"},
        is_fix=True,
    )
    assert decision.verdict == "reject"
    assert decision.rule_results.get("R6_production_reachability") is False


# ============================================================
# X.I-3: TaskSpec.production_entry_changes_required field
# ============================================================


def test_task_spec_has_production_entry_changes_field() -> None:
    """Pydantic model accepts the new field; default is empty list."""
    from kun.datamodel.task import TaskSpec

    spec = TaskSpec(goal_detail="...")
    assert spec.production_entry_changes_required == []

    spec2 = TaskSpec(
        goal_detail="...",
        production_entry_changes_required=[
            "kun/engineering/orchestrator.py",
            "kun/control_plane/daemon.py",
        ],
    )
    assert "kun/engineering/orchestrator.py" in spec2.production_entry_changes_required


# ============================================================
# X.I-4: bug_root_cause_cases past-hook lookup
# ============================================================


@pytest.mark.asyncio
async def test_past_hook_empty_when_no_db_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DB returns no matches, the lookup returns empty list."""
    from kun.integration.bug_root_cause_lookup import (
        lookup_similar_root_cause_cases,
    )

    # Stub the session_scope to return empty rows
    class _Empty:
        def all(self) -> list[Any]:
            return []

    class _FakeResult:
        def scalars(self) -> Any:
            return _Empty()

    class _FakeSession:
        async def execute(self, _: Any) -> _FakeResult:
            return _FakeResult()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_scope(**_: Any):
        yield _FakeSession()

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)

    findings = await lookup_similar_root_cause_cases(
        tenant_id="t-empty",
        recent_steps=[{"summary": "production wiring orphan"}],
    )
    assert findings == []


@pytest.mark.asyncio
async def test_past_hook_returns_scored_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DB has matching cases, hook returns them sorted by score."""
    from kun.integration.bug_root_cause_lookup import (
        lookup_similar_root_cause_cases,
    )

    class _FakeRow:
        def __init__(
            self,
            *,
            case_id: str,
            trace_signature: str,
            error_type: str,
            root_cause_kind: str,
            fix_pattern: str,
            hit_count: int = 1,
        ) -> None:
            self.case_id = case_id
            self.trace_signature = trace_signature
            self.error_type = error_type
            self.root_cause_kind = root_cause_kind
            self.fix_pattern = fix_pattern
            self.hit_count = hit_count

    rows = [
        _FakeRow(
            case_id="brc-1",
            trace_signature="production wiring orphan",
            error_type="orphan_capability",
            root_cause_kind="production_path_unwired",
            fix_pattern="wire to bundle + audit",
            hit_count=3,
        ),
        _FakeRow(
            case_id="brc-2",
            trace_signature="unrelated quantum compiler issue",
            error_type="other",
            root_cause_kind="other",
            fix_pattern="nope",
            hit_count=1,
        ),
    ]

    class _Scalars:
        def all(self) -> list[Any]:
            return rows

    class _FakeResult:
        def scalars(self) -> Any:
            return _Scalars()

    class _FakeSession:
        async def execute(self, _: Any) -> _FakeResult:
            return _FakeResult()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_scope(**_: Any):
        yield _FakeSession()

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)

    findings = await lookup_similar_root_cause_cases(
        tenant_id="t-match",
        recent_steps=[{"summary": "production wiring orphan capability"}],
    )
    assert len(findings) >= 1
    assert findings[0]["case_id"] == "brc-1"
    assert findings[0]["score"] > 0
    assert "orphan" in findings[0]["finding"].lower()


# ============================================================
# X.I-1: Auditor heuristic auto-escalates orphan capability
# ============================================================


@pytest.mark.asyncio
async def test_heuristic_auditor_escalates_when_target_module_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the heuristic auditor sees an unreachable target_module, it
    sets allow_release=False and adds an X.I-1 bypass method note."""
    from contextlib import asynccontextmanager
    from typing import Any as _Any

    captured_reports: list[_Any] = []

    @asynccontextmanager
    async def fake_scope(**_: _Any):
        class _S:
            def add(self, instance: _Any) -> None:
                captured_reports.append(instance)

            async def flush(self) -> None:
                return None

        yield _S()

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)
    # Force LLM auditor off so heuristic path runs
    monkeypatch.delenv("KUN_V7_AUDITOR_USE_LLM", raising=False)
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")

    from kun.agents.gate.service import GateDecision
    from kun.integration.auditor_report_v7_bridge import (
        emit_heuristic_auditor_report_for_capability,
    )

    decision = GateDecision(
        decision_id="dec-orphan-test",
        verdict="approve",
        reasons=["everything passed"],
        capability_id="cap-orphan-test",
        capability_row_payload=None,
        promotion_state="promoted",
        rule_results={
            "R1_test_report": True,
            "R2_diagnostic": True,
            "R3_debrief": True,
            "R4_self_referential": True,
        },
    )
    await emit_heuristic_auditor_report_for_capability(
        capability_id="cap-orphan-test",
        decision=decision,
        target_stage="candidate",
        tenant_id="t-xi1",
        target_module="OrphanedXYZWidget1234",
    )
    assert captured_reports, "auditor row was not written"
    row = captured_reports[0]
    # Heuristic auto-escalated: allow_release=False + bypass note mentions X.I-1
    assert row.allow_release is False
    assert any(
        "production-path unreachable" in m or "X.I-1" in m
        for m in (row.bypass_methods or [])
    )
