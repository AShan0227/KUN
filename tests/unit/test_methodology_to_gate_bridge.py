"""V7 Phase X.I-0b — methodology distill → Gate bridge tests.

Before X.I-0b, GateService.admit was an ORPHAN in production (only
``kun/integration/prompt_ab.py`` called it, and prompt_ab is not on
the user-facing WS path). Chained orphans:
  - V7 §15 lifecycle write side (lifecycle_transitions)
  - V7 §16.6 auditor write side (auditor_reports)

The bridge ``admit_methodology_candidate_via_gate`` flips Gate from
orphan to production-wired via ``MethodologyDistillStep`` (called from
idle_batch_worker which is launched in ``kun/api/main.py``).

This file asserts:
  1. Happy path: candidate → Gate.admit returns a verdict
  2. Synthetic payload includes the required keys
  3. Bridge disabled via env returns "bridge_disabled" without calling Gate
  4. Stable capability_id slug: same title → same id (idempotent)
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.integration.methodology_to_gate_bridge import (
    MethodologyAdmissionResult,
    admit_methodology_candidate_via_gate,
    build_admission_payload,
)

pytestmark = pytest.mark.asyncio


class _StubGateService:
    """Records its inputs + returns a configurable verdict."""

    def __init__(self, verdict: str = "approve") -> None:
        self.verdict = verdict
        self.last_args: dict[str, Any] = {}

    async def admit(
        self,
        experiment: dict[str, Any] | None,
        *,
        test_report: dict[str, Any] | None,
        diagnostic_record: dict[str, Any] | None = None,
        debrief: dict[str, Any] | None = None,
        is_fix: bool = False,
        tenant_id: str = "default",
    ) -> Any:
        from kun.agents.gate.service import GateDecision

        self.last_args = {
            "experiment": experiment,
            "test_report": test_report,
            "diagnostic_record": diagnostic_record,
            "debrief": debrief,
            "is_fix": is_fix,
            "tenant_id": tenant_id,
        }
        return GateDecision(
            decision_id="dec-stub-1",
            verdict=self.verdict,  # type: ignore[arg-type]
            reasons=[f"stub returns {self.verdict}"],
            capability_id="cap-stub-1" if self.verdict == "approve" else None,
            capability_row_payload=None,
            promotion_state=(
                "promoted" if self.verdict == "approve" else "rejected"
            ),
            rule_results={
                "R1_test_report": True,
                "R2_diagnostic": True,
                "R3_debrief": True,
                "R4_self_referential": True,
                "R5_evidence_quality": True,
            },
        )


def test_build_admission_payload_includes_required_keys() -> None:
    payload = build_admission_payload(
        candidate_title="test methodology title",
        candidate_topic="testing",
        candidate_rationale="this is the rationale",
        source_file="docs/dev_logs/X.H-progress.md",
    )
    assert "experiment" in payload
    assert "test_report" in payload
    assert "diagnostic_record" in payload
    assert "debrief" in payload
    assert payload["experiment"]["kind"] == "methodology"


def test_capability_id_is_stable_for_same_title() -> None:
    payload1 = build_admission_payload(
        candidate_title="foo bar baz",
        candidate_topic="t",
        candidate_rationale="r",
        source_file="s",
    )
    payload2 = build_admission_payload(
        candidate_title="foo bar baz",
        candidate_topic="t",
        candidate_rationale="r",
        source_file="s",
    )
    assert payload1["experiment"]["experiment_id"] == payload2["experiment"]["experiment_id"]


async def test_admit_methodology_candidate_routes_to_gate_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headline production-wiring test: the bridge calls Gate with the
    synthetic payload and returns a MethodologyAdmissionResult."""
    monkeypatch.setenv("KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv(
        "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "false"
    )  # don't fire DB writes in unit test

    gate = _StubGateService(verdict="approve")
    result = await admit_methodology_candidate_via_gate(
        candidate_title="my-new-methodology",
        candidate_topic="testing",
        candidate_rationale="rationale text",
        source_file="docs/dev_logs/X.H.md",
        tenant_id="t-bridge",
        gate_service=gate,
    )
    assert isinstance(result, MethodologyAdmissionResult)
    assert result.gate_verdict == "approve"
    assert result.capability_id is not None
    assert result.capability_id.startswith("methodology:")
    # Gate received the synthetic payload
    assert gate.last_args["experiment"]["candidate_change"] == "my-new-methodology"
    assert gate.last_args["tenant_id"] == "t-bridge"


async def test_bridge_env_disabled_skips_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED", "false")
    gate = _StubGateService()
    result = await admit_methodology_candidate_via_gate(
        candidate_title="x",
        candidate_topic="t",
        candidate_rationale="r",
        source_file="s",
        gate_service=gate,
    )
    assert result.gate_verdict == "bridge_disabled"
    # Gate was NOT called when env disabled
    assert gate.last_args == {}


async def test_gate_reject_yields_no_capability_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv(
        "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED", "false"
    )
    gate = _StubGateService(verdict="reject")
    result = await admit_methodology_candidate_via_gate(
        candidate_title="bad-methodology",
        candidate_topic="t",
        candidate_rationale="r",
        source_file="s",
        gate_service=gate,
    )
    assert result.gate_verdict == "reject"
    assert result.capability_id is None
    assert result.lifecycle_emitted is False
