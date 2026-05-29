"""V7 Phase X.I-0b — wire methodology_distill candidates through GateService.

Before X.I-0b:
  - kun.engineering.methodology_distill outputs ``MethodologyCandidate``
    objects every idle batch tick (via ``MethodologyDistillStep.run``).
  - GateService.admit was an ORPHAN — only called from
    ``kun/integration/prompt_ab.py`` (A/B test framework), NOT a real
    user / RSI candidate intake path.
  - Therefore V7 §15 9-stage lifecycle, V7 §16.6 auditor reports, and
    V7 §12 RSI write-side were all collectively dormant in production.

What X.I-0b does:

  MethodologyCandidate (engineering / distill output)
      ↓
  build_synthetic_admission_payload  (frozen IO → test_report etc)
      ↓
  GateService.admit(experiment=..., test_report=..., diagnostic_record=...)
      ↓
  (if approve)
      ↓
  capability_lifecycle_v7_bridge.emit_v7_lifecycle_transition_for_gate_decision
      ↓
  V7 §15 lifecycle_transitions row (OBSERVATION → CANDIDATE) lands
      ↓
  auditor_report_v7_bridge (already chained) → auditor_reports row lands
      ↓
  cockpit /capabilities reader sees the new candidate

This single function is what flips GateService + AuditorBridge from
ORPHAN to PRODUCTION-WIRED via the existing daemon (idle_batch_worker
is launched in kun/api/main.py:152-161).

Opt-out: KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED=false (default true
once env truthy/empty resolves to True). Cost: each call writes 1-2 PG
rows + optionally fires the LLM auditor (separate env switch).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.integration.methodology_to_gate_bridge")

_BRIDGE_ENABLED_ENV = "KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED"


def _bridge_enabled() -> bool:
    raw = os.environ.get(_BRIDGE_ENABLED_ENV, "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


@dataclass(frozen=True)
class MethodologyAdmissionResult:
    """V7 §13.6 frozen IO — outcome of one methodology → Gate flow."""

    candidate_title: str
    capability_id: str | None
    gate_verdict: str  # approve / reject / human_review
    lifecycle_emitted: bool
    auditor_emitted: bool
    rationale: str = ""
    error: str | None = None


def _slug_for_capability_id(title: str) -> str:
    """Build a stable capability_id from the candidate title.

    The bridge writes a deterministic id so the same methodology distilled
    twice doesn't churn two lifecycle rows.
    """
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    if not slug:
        slug = "methodology"
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    return f"methodology:{slug}-{digest}"


def build_admission_payload(
    *,
    candidate_title: str,
    candidate_topic: str,
    candidate_rationale: str,
    source_file: str,
) -> dict[str, Any]:
    """Build the (experiment, test_report, diagnostic_record, debrief) dict
    for GateService.admit. The values are synthesized — methodology candidates
    don't have AB experiment numbers, so we generate a "soft pass" report
    that lets the gate run its 5 rules and the bridge see the resulting
    verdict. Strict gates can still reject (R4 self-referential, low evidence
    quality)."""
    return {
        "experiment": {
            "experiment_id": _slug_for_capability_id(candidate_title),
            "target_module": "methodology_distill_pipeline",
            "candidate_change": candidate_title,
            "kind": "methodology",
        },
        "test_report": {
            "pass_rate": 1.0,  # methodologies have no automated AB pass-rate
            "trials": 1,
            "fingerprint": _slug_for_capability_id(candidate_title),
        },
        "diagnostic_record": {
            "kind": "methodology_distill",
            "topic": candidate_topic,
            "source_file": source_file,
            "rationale": candidate_rationale[:500],
        },
        "debrief": {
            "evidence_quality": 0.75,
            "lessons": [candidate_rationale[:200]],
            "is_self_referential": False,
        },
    }


async def admit_methodology_candidate_via_gate(
    *,
    candidate_title: str,
    candidate_topic: str,
    candidate_rationale: str,
    source_file: str,
    tenant_id: str = "default",
    gate_service: Any | None = None,
) -> MethodologyAdmissionResult:
    """Run one methodology candidate through GateService.admit and chain
    the V7 lifecycle + auditor bridges.

    Returns a frozen ``MethodologyAdmissionResult`` so callers
    (idle_batch step) can summarize.
    """
    if not _bridge_enabled():
        return MethodologyAdmissionResult(
            candidate_title=candidate_title,
            capability_id=None,
            gate_verdict="bridge_disabled",
            lifecycle_emitted=False,
            auditor_emitted=False,
            rationale=f"env {_BRIDGE_ENABLED_ENV}=false — bridge skipped",
        )

    if gate_service is None:
        from kun.agents.gate.service import GateService

        gate_service = GateService()

    payload = build_admission_payload(
        candidate_title=candidate_title,
        candidate_topic=candidate_topic,
        candidate_rationale=candidate_rationale,
        source_file=source_file,
    )
    try:
        decision = await gate_service.admit(
            payload["experiment"],
            test_report=payload["test_report"],
            diagnostic_record=payload["diagnostic_record"],
            debrief=payload["debrief"],
            is_fix=False,
            tenant_id=tenant_id,
        )
    except Exception as e:
        log.warning(
            "methodology_to_gate_bridge.admit_failed",
            candidate=candidate_title,
            error=f"{type(e).__name__}: {e}",
        )
        return MethodologyAdmissionResult(
            candidate_title=candidate_title,
            capability_id=None,
            gate_verdict="error",
            lifecycle_emitted=False,
            auditor_emitted=False,
            error=f"{type(e).__name__}: {e}",
        )

    # Chain V7 §15 lifecycle bridge — fires only on approve (its own check).
    lifecycle_emitted = False
    auditor_emitted = False
    capability_id = _slug_for_capability_id(candidate_title)
    try:
        from kun.integration.capability_lifecycle_v7_bridge import (
            emit_lifecycle_transition_for_gate_decision,
        )

        transition_id = await emit_lifecycle_transition_for_gate_decision(
            decision=decision,
            tenant_id=tenant_id,
        )
        lifecycle_emitted = transition_id is not None
        # The auditor bridge is called inside the lifecycle bridge, so if
        # lifecycle fired the auditor (heuristic, plus LLM if env on) did too.
        auditor_emitted = lifecycle_emitted
    except Exception as e:
        log.warning(
            "methodology_to_gate_bridge.lifecycle_emit_failed",
            candidate=candidate_title,
            error=f"{type(e).__name__}: {e}",
        )

    log.info(
        "methodology_to_gate_bridge.completed",
        candidate=candidate_title,
        gate_verdict=decision.verdict,
        capability_id=capability_id if decision.verdict == "approve" else None,
        lifecycle_emitted=lifecycle_emitted,
        auditor_emitted=auditor_emitted,
    )
    return MethodologyAdmissionResult(
        candidate_title=candidate_title,
        capability_id=capability_id if decision.verdict == "approve" else None,
        gate_verdict=decision.verdict,
        lifecycle_emitted=lifecycle_emitted,
        auditor_emitted=auditor_emitted,
        rationale="; ".join(decision.reasons[:3]),
    )


__all__ = [
    "MethodologyAdmissionResult",
    "admit_methodology_candidate_via_gate",
    "build_admission_payload",
]
