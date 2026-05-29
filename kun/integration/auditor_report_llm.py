"""V7 §16.6 — real LLM-driven 7-角度 auditor (MF-AR-LLM).

X.B.MF-AR-wiring landed a *heuristic* auditor that classified risk from
``GateDecision.rule_results``. The auditor row carried
``auditor_provider="heuristic/gate-derived"`` so consumers could tell it
apart from a real audit.

This module is the **real** audit:
  - Renders the V7 §16.6 7-角度 AUDITOR_SYSTEM_PROMPT_TEMPLATE
  - Sends it through ``ensemble_invoke`` (cross-family gpt-5.5 + Qwen)
  - Parses the JSON output via ``parse_auditor_json`` (single source of
    schema truth — kun/integration/auditor_report_db.py)
  - Writes ``AuditorReportRow`` with ``auditor_provider=<consensus winner>``
    so cockpit can filter heuristic vs real

Falls back to heuristic when:
  - ``KUN_V7_AUDITOR_USE_LLM=false`` (default false — opt-in to spend $)
  - Ensemble unavailable (no providers configured, or factory returns None)
  - LLM call raises (network / cost cap / etc.) — logged + swallowed
  - LLM returns invalid JSON — logged + fallback

Cost: ~$0.10-0.30 per audit (1 ensemble call, ~1000 input tokens, ~300 output)
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kun.core.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("kun.integration.auditor_report_llm")


_USE_LLM_ENV = "KUN_V7_AUDITOR_USE_LLM"


def _llm_audit_enabled() -> bool:
    """Default OFF — opt-in to spend $ on LLM audits."""
    raw = os.environ.get(_USE_LLM_ENV, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _extract_json_block(text: str) -> str | None:
    """Pull the first {...} JSON block from an LLM response. LLMs sometimes
    wrap output in ```json fences or add prose around it."""
    # Strip code fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    # Find first balanced {...} block
    start = text.find("{")
    if start < 0:
        return None
    # Naive balance check (LLM JSON is usually clean)
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


async def llm_audit_capability(
    *,
    capability_id: str,
    capability_name: str,
    design_promise: str,
    code_paths_to_audit: list[str],
    test_files_to_audit: list[str],
    tenant_id: str = "default",
    recent_dogfood_summary: str | None = None,
) -> str | None:
    """Run a real V7 §16.6 7-角度 attacker audit via LLM ensemble.

    Returns the report_id when emitted, None when:
      - env-disabled (caller should fall back to heuristic)
      - factory failed to build ensemble (no providers)
      - LLM raised / returned invalid JSON

    Side effect: writes one row to auditor_reports table with
    auditor_provider=<ensemble consensus winner>.
    """
    if not _llm_audit_enabled():
        log.debug("auditor_report_llm.disabled", env=_USE_LLM_ENV)
        return None

    # Build the ensemble invoker — same pattern as MF-2
    try:
        from kun.integration.ensemble_invoker_factory import (
            build_ensemble_invoker_from_settings,
        )
        from kun.interface.llm.router import get_router

        router = get_router()
        invoker = build_ensemble_invoker_from_settings(
            router=router,
            purpose="critique",  # 区分 audit traffic vs execution
            tenant_id=tenant_id,
            temperature=0.3,  # lower for stricter audit
            max_tokens=2048,
        )
        if invoker is None:
            log.warning(
                "auditor_report_llm.ensemble_unavailable",
                hint="set KUN_V7_ENSEMBLE_ENABLED + tiers, or fallback heuristic",
            )
            return None
    except Exception as e:
        log.warning(
            "auditor_report_llm.factory_failed",
            error=f"{type(e).__name__}: {e}",
        )
        return None

    # Render the V7 §16.6 prompt
    from kun.governance.production_path_traceability import (
        check_symbol_reachable,
    )
    from kun.integration.external_supervisor_critique import (
        render_auditor_prompt,
    )

    # X.I-1 — compute production-path reachability automatically; feed
    # the result to the LLM auditor so it doesn't have to grep itself.
    reach = check_symbol_reachable(capability_name)
    production_path_check = {
        "symbol": reach.symbol,
        "reachable": reach.reachable,
        "entries_hit": reach.entries_hit,
    }

    prompt = render_auditor_prompt(
        capability_name=capability_name,
        design_doc_excerpt=design_promise,
        code_paths_to_audit=code_paths_to_audit,
        test_files_to_audit=test_files_to_audit,
        recent_dogfood_summary=recent_dogfood_summary,
        production_path_check=production_path_check,
    )

    # Run the audit
    try:
        log.info(
            "auditor_report_llm.invoking",
            capability_id=capability_id,
            capability_name=capability_name,
            n_code_paths=len(code_paths_to_audit),
            n_test_files=len(test_files_to_audit),
        )
        step = await invoker(
            [
                {
                    "role": "system",
                    "content": (
                        "You are KUN's V7 §16.6 production-loop attacker auditor. "
                        "Output strict JSON per the schema in the prompt. No prose."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
    except Exception as e:
        log.warning(
            "auditor_report_llm.invoke_failed",
            capability_id=capability_id,
            error=f"{type(e).__name__}: {e}",
        )
        return None

    # Parse the LLM's JSON output
    raw_json_str = _extract_json_block(step.content or "")
    if not raw_json_str:
        log.warning(
            "auditor_report_llm.no_json_block_in_response",
            capability_id=capability_id,
            content_excerpt=(step.content or "")[:200],
        )
        return None

    try:
        raw = json.loads(raw_json_str)
    except json.JSONDecodeError as e:
        log.warning(
            "auditor_report_llm.json_decode_failed",
            capability_id=capability_id,
            error=str(e),
            content_excerpt=raw_json_str[:200],
        )
        return None

    # Build + persist AuditorReport
    try:
        from kun.core.ids import new_id
        from kun.integration.auditor_report_db import (
            make_auditor_report_emitter,
            parse_auditor_json,
        )

        report = parse_auditor_json(
            raw_json=raw,
            report_id=new_id("plan_review"),
            audited_capability=capability_name,
            auditor_provider="ensemble/v7-7-angle-attacker",
            audited_at=datetime.now(UTC),
        )
    except Exception as e:
        log.warning(
            "auditor_report_llm.parse_or_invariant_failed",
            capability_id=capability_id,
            error=f"{type(e).__name__}: {e}",
            raw_json_keys=list(raw.keys()) if isinstance(raw, dict) else None,
        )
        return None

    try:
        emit = make_auditor_report_emitter(tenant_id)
        await emit(report)
    except Exception as e:
        log.warning(
            "auditor_report_llm.persist_failed",
            capability_id=capability_id,
            report_id=report.report_id,
            error=f"{type(e).__name__}: {e}",
        )
        return None

    log.info(
        "auditor_report_llm.persisted",
        capability_id=capability_id,
        report_id=report.report_id,
        risk_level=report.risk_level,
        allow_release=report.allow_release,
        n_bypass=len(report.bypass_methods),
        cost_usd=step.cost_usd,
    )
    return report.report_id


__all__ = [
    "llm_audit_capability",
]
