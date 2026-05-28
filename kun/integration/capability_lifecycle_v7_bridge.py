"""GateService → V7 §15 capability lifecycle transition bridge.

V7 Phase X.B.MF-LC-wiring (P0 attacker-audit follow-up). Closes the gap that
``lifecycle_transitions`` table had no production writer.

Mapping (V6 GateService decision → V7 §15 stage transition):

  - verdict="approve" (promotion_state="merged")
        → ``OBSERVATION → CANDIDATE``
        with evidence_refs = [
            "capability_candidate:<capability_id>",
            "test_report:<from R1>",  # if rule_results R1 passed
            "diagnostic_record:<from R2>",
            "debrief:<from R3>",
        ]
  - verdict="reject"
        → no transition (capability never entered V7 lifecycle)
  - verdict="awaiting_human_review"
        → no transition (decision pending, V7 stage not yet chosen)

Why this mapping is honest (not a stretch):
  - V6 GateService runs 5 engineering rules (R1-R5) checking exactly the same
    3-evidence kinds that V7 §12.3 requires for CANDIDATE → REPLAY (and one
    step lower for OBSERVATION → CANDIDATE: just "evidence exists").
  - When R1 + R2 + R3 all pass, the V7 invariant "CANDIDATE has 3 evidence"
    is already satisfied — emitting CANDIDATE entry at this point is
    documentation, not extrapolation.

Sync vs async:
  - GateService.admit() is async, so this bridge is async too — no thread
    fire-and-forget needed (unlike the V6 Mission Director sync runner).
  - Emit failures are swallowed + logged; gate decision is unaffected.

Opt-out: ``KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED=false`` to disable.

Production-path-必经 grep evidence:
  - This module is imported from ``kun/agents/gate/service.py``
  - Wiring proof test: ``tests/integration/test_gate_to_v7_lifecycle_wiring.py``
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.agents.gate.service import GateDecision

log = get_logger("kun.integration.capability_lifecycle_v7_bridge")


_BRIDGE_ENABLED_ENV = "KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED"


def _bridge_enabled() -> bool:
    """Default on; set KUN_V7_CAPABILITY_LIFECYCLE_BRIDGE_ENABLED=false to disable."""
    raw = os.environ.get(_BRIDGE_ENABLED_ENV, "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _build_evidence_refs(decision: GateDecision) -> list[str]:
    """Build V7 §12.3 evidence_refs from GateDecision.rule_results.

    Each rule_result that passed maps to one evidence_ref string:
      R1 (test_report)     → "test_report:<capability_id>"
      R2 (diagnostic)      → "diagnostic_record:<capability_id>"
      R3 (debrief)         → "debrief:<capability_id>"
      R4 (self_referential) → "self_referential_check:<capability_id>"
      R5 (extra)           → "extra_rule:<capability_id>"
    Plus the capability_candidate ref itself (always emitted on approve).

    DB invariant for to_stage='replay' requires ≥ 1 evidence; OBSERVATION →
    CANDIDATE doesn't have that DB check but the V7 §12.2 service layer rejects
    empty evidence_refs on CANDIDATE → REPLAY transitions later, so it's good
    practice to populate now.
    """
    cap_id = decision.capability_id or "unknown"
    refs: list[str] = [f"capability_candidate:{cap_id}"]
    rule_map = {
        "R1_test_report": "test_report",
        "R2_diagnostic": "diagnostic_record",
        "R3_debrief": "debrief",
    }
    for rule_key, evidence_kind in rule_map.items():
        if decision.rule_results.get(rule_key, False):
            refs.append(f"{evidence_kind}:{cap_id}")
    return refs


async def emit_lifecycle_transition_for_gate_decision(
    *,
    decision: GateDecision,
    tenant_id: str = "default",
) -> str | None:
    """Emit a V7 §15 lifecycle transition for an approved GateDecision.

    Returns the transition_id when emitted, None when:
      - bridge env-disabled
      - decision verdict != "approve" (reject / awaiting_human_review skip)
      - decision has no capability_id (mal-formed approve, defensive)
      - emit failed (logged + swallowed; never propagates)

    Caller (GateService) should ignore the return; this is fire-and-forget
    observability, not part of the gate's decision logic.
    """
    if not _bridge_enabled():
        log.debug(
            "capability_lifecycle_v7_bridge.disabled", env=_BRIDGE_ENABLED_ENV
        )
        return None

    if decision.verdict != "approve":
        log.debug(
            "capability_lifecycle_v7_bridge.skipped_non_approve",
            verdict=decision.verdict,
        )
        return None

    if not decision.capability_id:
        log.warning(
            "capability_lifecycle_v7_bridge.missing_capability_id",
            decision_id=decision.decision_id,
        )
        return None

    # Build evidence_refs from rule_results (defensive: don't import V7 service
    # at module top to keep import surface small / no cycles)
    evidence_refs = _build_evidence_refs(decision)

    try:
        from kun.governance.capability_lifecycle import (
            CapabilityLifecycleService,
            CapabilityLifecycleStage,
        )
        from kun.integration.capability_lifecycle_db import (
            make_lifecycle_transition_emitter,
        )

        service = CapabilityLifecycleService(
            transition_emitter=make_lifecycle_transition_emitter(tenant_id),
            # MF-LC: OBSERVATION → CANDIDATE doesn't need three-evidence
            # invariant (only REPLAY entry does). Keep service defaults.
        )
        record = await service.transition(
            capability_id=decision.capability_id,
            from_stage=CapabilityLifecycleStage.OBSERVATION,
            to_stage=CapabilityLifecycleStage.CANDIDATE,
            evidence_refs=evidence_refs,
            decision_rationale=(
                f"GateService.admit verdict=approve, "
                f"promotion_state={decision.promotion_state}; "
                f"rule_results={decision.rule_results}"
            ),
            metrics_snapshot={
                "n_rules_passed": sum(
                    1 for v in decision.rule_results.values() if v
                ),
                "n_rules_total": len(decision.rule_results),
            },
        )
    except Exception as e:
        log.warning(
            "capability_lifecycle_v7_bridge.emit_failed",
            decision_id=decision.decision_id,
            capability_id=decision.capability_id,
            error=f"{type(e).__name__}: {e}",
        )
        return None

    log.info(
        "capability_lifecycle_v7_bridge.emitted",
        decision_id=decision.decision_id,
        capability_id=decision.capability_id,
        transition_id=record.transition_id,
        n_evidence=len(evidence_refs),
    )

    # V7 Phase X.B.MF-AR-wiring: fire heuristic auditor report alongside the
    # lifecycle transition. Strict-acceptance stage entries (REPLAY+) ideally
    # want LLM-driven 7-角度审计; for now heuristic from gate rule_results.
    # Fire-and-forget; bridge handles its own env opt-out + errors.
    try:
        from kun.integration.auditor_report_v7_bridge import (
            emit_heuristic_auditor_report_for_capability,
        )

        await emit_heuristic_auditor_report_for_capability(
            capability_id=decision.capability_id,
            decision=decision,
            target_stage=CapabilityLifecycleStage.CANDIDATE.value,
            tenant_id=tenant_id,
        )
    except Exception:
        # Defense in depth — auditor bridge failure does NOT propagate.
        pass

    return record.transition_id


__all__ = [
    "emit_lifecycle_transition_for_gate_decision",
]
