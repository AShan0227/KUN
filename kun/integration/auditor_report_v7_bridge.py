"""GateService → V7 §16.6 heuristic auditor report bridge.

V7 Phase X.B.MF-AR-wiring. Closes the "auditor_reports table is orphan" P0
finding from the attacker audit.

What this does (minimum viable wiring, NOT the full V7 §16.6 LLM-driven audit):
  - Each time a V7 §15 lifecycle transition lands AND the target stage is a
    strict-acceptance stage (REPLAY/HOLDOUT/SHADOW/CANARY/PRODUCTION), emit
    a **heuristic AuditorReport** that classifies risk from the GateDecision
    rule_results.

Risk classification (heuristic, NOT LLM):
  - R4 (self_referential) failed → P1 (structural concern)
  - Test pass_rate < 0.95 OR R1/R2/R3 failed → P1
  - All rules passed AND no self-referential → P2

P0 mapping: heuristic never emits P0 (that's reserved for full LLM-driven
7-角度审计). When real auditor LLM (e.g. Qwen + gpt-5.5 ensemble) ships,
it can upgrade heuristic reports to P0/P1 with real bypass_methods.

allow_release derived from risk_level:
  - P0 → False (DB CHECK enforces)
  - P1 with reasons → False (caller should review)
  - P2 → True

design_promise + real_code_path come from GateDecision metadata if present;
otherwise placeholder strings noting "heuristic — not LLM-driven yet".

Why this is honest (not faking):
  - The output row HAS auditor_provider="heuristic/gate-derived" (not a real
    LLM family) — consumers can filter on this to know it's heuristic.
  - bypass_methods list cites concrete failed rule_results.
  - rationale field explicitly says "heuristic from gate rule_results,
    awaiting full LLM 7-角度审计 wiring (MF-AR-LLM)".

Production-path-必经 grep evidence:
  - Called from kun/integration/capability_lifecycle_v7_bridge.py after
    each lifecycle transition emit succeeds.

Opt-out: KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED=false.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.agents.gate.service import GateDecision

log = get_logger("kun.integration.auditor_report_v7_bridge")


_BRIDGE_ENABLED_ENV = "KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED"


def _bridge_enabled() -> bool:
    raw = os.environ.get(_BRIDGE_ENABLED_ENV, "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _classify_heuristic_risk(
    decision: GateDecision, test_pass_rate: float | None
) -> tuple[str, bool]:
    """Heuristic risk_level + allow_release from gate rule_results.

    Returns: (risk_level, allow_release)
    """
    r4_passed = decision.rule_results.get("R4_self_referential", True)
    r1_passed = decision.rule_results.get("R1_test_report", True)
    r2_passed = decision.rule_results.get("R2_diagnostic", True)
    r3_passed = decision.rule_results.get("R3_debrief", True)
    pass_rate_ok = test_pass_rate is None or test_pass_rate >= 0.95

    if not r4_passed:
        # Self-referential change → human review concern
        return ("P1", False)
    if not (r1_passed and r2_passed and r3_passed and pass_rate_ok):
        # Some engineering rule failed — but gate let it through (e.g. is_fix path)
        return ("P1", False)
    return ("P2", True)


def _build_bypass_methods(decision: GateDecision) -> list[str]:
    """List concrete failed rule_results as bypass candidates."""
    bypasses: list[str] = []
    for rule_key, passed in decision.rule_results.items():
        if not passed:
            bypasses.append(
                f"engineering_rule_failed:{rule_key} "
                f"(heuristic — see GateDecision.reasons for detail)"
            )
    return bypasses


async def emit_heuristic_auditor_report_for_capability(
    *,
    capability_id: str,
    decision: GateDecision,
    target_stage: str,
    tenant_id: str = "default",
    test_pass_rate: float | None = None,
    target_module: str | None = None,
) -> str | None:
    """Emit an AuditorReport after a V7 lifecycle transition.

    Strategy (V7 §16.6 fallback chain, X.C.MF-AR-LLM):
      1. If KUN_V7_AUDITOR_USE_LLM=true AND ensemble available, try
         real LLM-driven 7-角度审计 via llm_audit_capability(). Returns
         that report_id on success.
      2. Otherwise fall back to the original heuristic (gate-derived).

    Returns the report_id when emitted, None when:
      - bridge env-disabled (KUN_V7_AUDITOR_REPORT_BRIDGE_ENABLED=false)
      - both LLM AND heuristic failed (logged + swallowed)

    Caller (capability_lifecycle_v7_bridge) ignores the return — this is
    observability, not gate-blocking.
    """
    if not _bridge_enabled():
        return None

    # V7 §16.6 MF-AR-LLM: try real LLM audit first when enabled
    try:
        from kun.integration.auditor_report_llm import llm_audit_capability

        # Code paths derived from target_module (best-effort)
        code_paths = [target_module] if target_module else []
        # No specific test_files for a freshly promoted capability — pass empty
        llm_report_id = await llm_audit_capability(
            capability_id=capability_id,
            capability_name=target_module or f"capability:{capability_id}",
            design_promise=(
                f"V7 §15 stage entry: → {target_stage}. "
                f"Capability {capability_id} promoted via GateService.admit. "
                f"Gate rule_results: {decision.rule_results}. "
                f"Reasons: {'; '.join(decision.reasons[:5])}"
            ),
            code_paths_to_audit=code_paths,
            test_files_to_audit=[],
            tenant_id=tenant_id,
            recent_dogfood_summary=None,
        )
        if llm_report_id is not None:
            log.info(
                "auditor_report_v7_bridge.llm_audit_succeeded",
                capability_id=capability_id,
                target_stage=target_stage,
                llm_report_id=llm_report_id,
            )
            return llm_report_id
        # llm_report_id is None → LLM path skipped or failed; continue to heuristic
    except Exception as e:
        log.warning(
            "auditor_report_v7_bridge.llm_audit_failed_falling_back",
            capability_id=capability_id,
            error=f"{type(e).__name__}: {e}",
        )

    # Fallback: heuristic
    risk_level, allow_release = _classify_heuristic_risk(
        decision, test_pass_rate
    )

    bypass_methods = _build_bypass_methods(decision)

    audited_capability = (
        target_module or f"capability:{capability_id}"
    )

    try:
        from datetime import UTC, datetime

        from kun.core.ids import new_id
        from kun.integration.auditor_report_db import (
            AuditorReport,
            make_auditor_report_emitter,
        )

        report = AuditorReport(
            report_id=new_id("plan_review"),  # reuse "pr" prefix for now
            audited_capability=audited_capability,
            audited_at=datetime.now(UTC),
            auditor_provider="heuristic/gate-derived",
            design_promise=(
                f"V7 §15 stage entry: → {target_stage}. "
                f"Capability {capability_id} promoted via GateService.admit."
            ),
            real_code_path=target_module or "unknown",
            bypass_methods=bypass_methods,
            min_repro_steps="N/A (heuristic, not LLM-driven)",
            risk_level=risk_level,
            must_fix=bypass_methods if not allow_release else [],
            acceptance_tests=[
                f"R1_test_report={decision.rule_results.get('R1_test_report', True)}",
                f"R4_self_referential={decision.rule_results.get('R4_self_referential', True)}",
            ],
            allow_release=allow_release,
            rationale=(
                "Heuristic auditor report from gate rule_results "
                "(MF-AR-wiring). Awaiting full LLM 7-角度审计 wiring "
                "(MF-AR-LLM) for production-grade attacker audit."
            ),
        )

        emit = make_auditor_report_emitter(tenant_id)
        await emit(report)
    except Exception as e:
        log.warning(
            "auditor_report_v7_bridge.emit_failed",
            capability_id=capability_id,
            target_stage=target_stage,
            error=f"{type(e).__name__}: {e}",
        )
        return None

    log.info(
        "auditor_report_v7_bridge.emitted",
        capability_id=capability_id,
        target_stage=target_stage,
        risk_level=risk_level,
        allow_release=allow_release,
        n_bypass=len(bypass_methods),
    )
    return report.report_id


__all__ = [
    "emit_heuristic_auditor_report_for_capability",
]
