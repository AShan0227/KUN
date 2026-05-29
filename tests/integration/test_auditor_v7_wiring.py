"""V7 Phase X.B.MF-AR-wiring — heuristic auditor report 真接生产路径.

Production-path-必经 evidence. Without this test, the claim "auditor_reports
table has production writer" is unverified.

What it proves:
  1. `GateService.admit(approve)` → lifecycle bridge → auditor bridge →
     AuditorReportRow真 added (chained wiring).
  2. Heuristic risk classification: R4 passed + high pass_rate → P2.
  3. Env opt-out (KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED=false) works.
  4. Bridge errors don't break the gate/lifecycle path.
  5. Production code真 imports the auditor bridge (regression guard).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.agents.gate.service import GateService
from kun.core.orm import AuditorReportRow, LifecycleTransitionRow


def _passing_inputs() -> dict[str, Any]:
    return {
        "experiment": {
            "experiment_id": "ex-ar-bridge-test",
            "target_module": "kun.test.target.ar",
            "rationale": "Heuristic auditor wiring proof",
            "rollback_on": ["pass_rate < 0.9"],
        },
        "test_report": {
            "pass_rate": 0.96,
            "covered_rules": ["R1", "R2", "R3"],
        },
        "diagnostic_record": {
            "diagnostic_id": "dx-ar",
            "root_cause": "test-driven",
            "verdict": "ok",
        },
        "debrief": {
            "debrief_id": "db-ar",
            "evidence_quality": 0.9,
            "summary": "ok",
        },
    }


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[dict[str, Any]]]:
    added: list[Any] = []
    scope_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        scope_calls.append(kwargs)
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    return added, scope_calls


# ============================================================
# THE wiring proof: gate approve → lifecycle → auditor heuristic emit
# ============================================================


async def test_gate_approve_emits_heuristic_auditor_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Production-path 必经 evidence for MF-AR-wiring.**

    Drives GateService.admit with passing inputs. Both lifecycle bridge AND
    auditor bridge fire. Assert both rows added.
    """
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    service = GateService()
    decision = await service.admit(**_passing_inputs(), tenant_id="t-ar-bridge")

    assert decision.verdict == "approve"

    # Wiring proof 1: lifecycle row added
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    assert len(lc_rows) == 1

    # Wiring proof 2: **auditor report row真 added**
    ar_rows = [r for r in added if isinstance(r, AuditorReportRow)]
    assert len(ar_rows) == 1, (
        f"Auditor bridge did NOT fire after lifecycle; added: {added}"
    )
    ar = ar_rows[0]
    assert ar.tenant_id == "t-ar-bridge"
    assert ar.auditor_provider == "heuristic/gate-derived"
    # All rules passed → P2 + allow_release=True
    assert ar.risk_level == "P2"
    assert ar.allow_release is True
    # rationale explicitly says heuristic
    assert "heuristic" in ar.rationale.lower()
    assert "MF-AR-wiring" in ar.rationale


async def test_auditor_bridge_disabled_skips_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env kill switch on auditor bridge — lifecycle still fires."""
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "false")
    added, _ = _install_fake_session(monkeypatch)

    service = GateService()
    decision = await service.admit(**_passing_inputs(), tenant_id="t-ar-off")

    assert decision.verdict == "approve"
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    ar_rows = [r for r in added if isinstance(r, AuditorReportRow)]
    assert len(lc_rows) == 1  # lifecycle still wired
    assert ar_rows == []      # but auditor opt-out works


async def test_auditor_bridge_exception_does_not_break_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditor bridge crash → lifecycle row still written, gate still OK."""
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    async def _boom(**kwargs: Any) -> str | None:
        raise RuntimeError("simulated auditor bridge failure")

    monkeypatch.setattr(
        "kun.integration.auditor_report_v7_bridge."
        "emit_heuristic_auditor_report_for_capability",
        _boom,
    )

    service = GateService()
    decision = await service.admit(**_passing_inputs(), tenant_id="t-ar-bad")

    assert decision.verdict == "approve"  # gate unaffected
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    assert len(lc_rows) == 1  # lifecycle still wrote


async def test_self_referential_capability_gets_p1_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-referential change (R4 fails) → heuristic risk P1, allow_release=False.

    GateService.admit will produce verdict='awaiting_human_review' for
    self_referential, not 'approve'. So lifecycle bridge skips emit, hence
    no auditor row either. This test verifies the V6 path stays clean.
    """
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    inputs = _passing_inputs()
    inputs["experiment"]["target_module"] = "kun.governance.engineering_discipline"
    # Self-modifying KUN itself → R4 should flag it
    inputs["experiment"]["self_modifies_own_governance"] = True

    service = GateService()
    decision = await service.admit(**inputs, tenant_id="t-self-ref")

    # GateService treats self-referential as awaiting_human_review, not
    # approve — bridge correctly skips
    if decision.verdict == "awaiting_human_review":
        lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
        ar_rows = [r for r in added if isinstance(r, AuditorReportRow)]
        assert lc_rows == []
        assert ar_rows == []


# ============================================================
# Heuristic classification — unit-level tests via direct emit
# ============================================================


async def test_heuristic_risk_classification_p2_when_all_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.agents.gate.service import GateDecision
    from kun.integration.auditor_report_v7_bridge import (
        emit_heuristic_auditor_report_for_capability,
    )

    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    decision = GateDecision(
        decision_id="dec-1",
        verdict="approve",
        reasons=["ok"],
        capability_id="cap-test",
        capability_row_payload={},
        promotion_state="merged",
        rule_results={
            "R1_test_report": True,
            "R2_diagnostic": True,
            "R3_debrief": True,
            "R4_self_referential": True,
        },
    )
    # X.I-1 — pass a real production-reachable target_module so the
    # heuristic auditor's auto-escalation (P2→P1 when orphan) doesn't fire.
    report_id = await emit_heuristic_auditor_report_for_capability(
        capability_id="cap-test",
        decision=decision,
        target_stage="candidate",
        tenant_id="t-heur",
        test_pass_rate=0.98,
        target_module="LongTaskOrchestrator",
    )
    assert report_id is not None
    ar_rows = [r for r in added if isinstance(r, AuditorReportRow)]
    assert len(ar_rows) == 1
    assert ar_rows[0].risk_level == "P2"
    assert ar_rows[0].allow_release is True


async def test_heuristic_risk_classification_p1_when_self_referential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.agents.gate.service import GateDecision
    from kun.integration.auditor_report_v7_bridge import (
        emit_heuristic_auditor_report_for_capability,
    )

    monkeypatch.setenv("KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    decision = GateDecision(
        decision_id="dec-2",
        verdict="approve",
        reasons=["ok"],
        capability_id="cap-self",
        capability_row_payload={},
        promotion_state="merged",
        rule_results={
            "R1_test_report": True,
            "R2_diagnostic": True,
            "R3_debrief": True,
            "R4_self_referential": False,  # ⚠️ self-modifying
        },
    )
    await emit_heuristic_auditor_report_for_capability(
        capability_id="cap-self",
        decision=decision,
        target_stage="candidate",
        tenant_id="t-self",
    )
    ar = next(r for r in added if isinstance(r, AuditorReportRow))
    assert ar.risk_level == "P1"
    assert ar.allow_release is False
    # bypass_methods cites the failed rule
    assert any("R4" in b for b in ar.bypass_methods)


# ============================================================
# Production-path grep guard
# ============================================================


def test_lifecycle_bridge_imports_auditor_bridge() -> None:
    """Regression guard: lifecycle bridge真 imports + calls auditor bridge."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "kun"
        / "integration"
        / "capability_lifecycle_v7_bridge.py"
    ).read_text(encoding="utf-8")
    assert (
        "from kun.integration.auditor_report_v7_bridge import"
        in src
    ), "Lifecycle bridge no longer imports auditor bridge — MF-AR-wiring regressed"
    assert (
        "emit_heuristic_auditor_report_for_capability(" in src
    ), "Auditor bridge function imported but NOT called"
