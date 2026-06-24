"""V7 Phase X.B.MF-LC-wiring — GateService → V7 §15 lifecycle 真接生产路径.

Production-path-必经 evidence (per V7 §16.6 attacker audit). Without this
test the claim "lifecycle_transitions table has production writer" is
unverified.

What it proves:
  1. `GateService.admit(...)` returning verdict='approve' emits a real
     `LifecycleTransitionRow(OBSERVATION → CANDIDATE)` via the V7 bridge.
  2. verdict='reject' → no transition emitted (correct skip).
  3. Bridge env kill-switch (KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED=false)
     truly disables emit even on approve.
  4. Bridge errors do NOT break the GateService approve path.
  5. Production code真 imports the bridge (regression guard, MF-1 pattern).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.agents.gate.service import GateService
from kun.core.orm import LifecycleTransitionRow

pytestmark = pytest.mark.integration

# ============================================================
# Fixtures: passing inputs to GateService.admit
# ============================================================


def _passing_inputs() -> dict[str, Any]:
    """Minimum inputs that make GateService.admit return verdict='approve'."""
    return {
        "experiment": {
            "experiment_id": "ex-bridge-test",
            "target_module": "kun.test.target",
            "rationale": "Bridge wiring proof for X.B.MF-LC-wiring",
            "rollback_on": ["pass_rate < 0.9"],
        },
        "test_report": {
            "pass_rate": 0.95,
            "covered_rules": ["R1", "R2", "R3"],
        },
        "diagnostic_record": {
            "diagnostic_id": "dx-1",
            "root_cause": "test-driven",
            "verdict": "ok",
        },
        "debrief": {
            "debrief_id": "db-1",
            "evidence_quality": 0.85,
            "summary": "ok",
        },
    }


# ============================================================
# Fake session capture (same shape as other Phase X.B tests)
# ============================================================


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
# THE wiring proof: GateService.admit(approve) → LifecycleTransitionRow added
# ============================================================


async def test_gate_approve_emits_v7_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The production-path-必经 evidence for MF-LC-wiring.**

    Drives GateService.admit (the EXACT production class) with inputs that
    pass all 5 rules. Asserts a real LifecycleTransitionRow hits the fake
    session (= would hit `lifecycle_transitions` table in prod).
    """
    monkeypatch.setenv(
        "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true"
    )
    added, scope_calls = _install_fake_session(monkeypatch)

    service = GateService()
    decision = await service.admit(**_passing_inputs(), tenant_id="t-bridge-lc")

    assert decision.verdict == "approve", f"got {decision.verdict}: {decision.reasons}"
    assert decision.capability_id is not None

    # **Wiring proof**: V7 emitter真 fired
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    assert len(lc_rows) == 1, (
        f"V7 bridge did NOT emit a LifecycleTransitionRow; added: {added}"
    )
    row = lc_rows[0]
    assert row.tenant_id == "t-bridge-lc"
    assert row.capability_id == decision.capability_id
    assert row.from_stage == "observation"
    assert row.to_stage == "candidate"
    # Evidence refs include rule_results that passed
    assert any("test_report:" in r for r in row.evidence_refs)
    assert any("capability_candidate:" in r for r in row.evidence_refs)
    # session_scope used the right tenant_id
    assert {"tenant_id": "t-bridge-lc"} in scope_calls


async def test_gate_reject_does_not_emit_v7_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verdict='reject' → bridge correctly skips (no V7 stage entry)."""
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")
    added, _ = _install_fake_session(monkeypatch)

    inputs = _passing_inputs()
    # Sabotage pass_rate so R1 fails → reject
    inputs["test_report"]["pass_rate"] = 0.5

    service = GateService()
    decision = await service.admit(**inputs, tenant_id="t-bridge-reject")

    assert decision.verdict == "reject"
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    assert lc_rows == [], "Bridge fired on reject (wrong); should skip"


async def test_gate_approve_with_bridge_disabled_skips_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env opt-out: KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED=false."""
    monkeypatch.setenv(
        "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "false"
    )
    added, _ = _install_fake_session(monkeypatch)

    service = GateService()
    decision = await service.admit(**_passing_inputs(), tenant_id="t-off")

    assert decision.verdict == "approve"  # V6 gate still works
    lc_rows = [r for r in added if isinstance(r, LifecycleTransitionRow)]
    assert lc_rows == [], "Bridge fired despite env opt-out"


async def test_gate_approve_survives_bridge_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge raise must NOT break V6 gate decision."""
    monkeypatch.setenv("KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "true")

    # Stub the bridge entry to raise
    async def _boom(**kwargs: Any) -> str | None:
        raise RuntimeError("simulated bridge failure")

    monkeypatch.setattr(
        "kun.integration.capability_lifecycle_v7_bridge."
        "emit_lifecycle_transition_for_gate_decision",
        _boom,
    )

    service = GateService()
    # Should NOT raise — V6 gate decision returned cleanly
    decision = await service.admit(**_passing_inputs(), tenant_id="t-broken")
    assert decision.verdict == "approve"


# ============================================================
# Production-path grep guard
# ============================================================


def test_gate_service_imports_lifecycle_v7_bridge() -> None:
    """Regression guard: kun/agents/gate/service.py真 imports + calls the bridge."""
    from pathlib import Path

    gate_source = (
        Path(__file__).resolve().parent.parent.parent
        / "kun"
        / "agents"
        / "gate"
        / "service.py"
    ).read_text(encoding="utf-8")
    assert (
        "from kun.integration.capability_lifecycle_v7_bridge import"
        in gate_source
    ), (
        "kun/agents/gate/service.py does NOT import the V7 lifecycle bridge — "
        "MF-LC-wiring regressed"
    )
    assert "emit_lifecycle_transition_for_gate_decision(" in gate_source, (
        "Bridge function imported but NOT called — wiring still broken"
    )
