"""L2.6 — RCDH 4 级诊断 + narrow_scope 单测."""

from __future__ import annotations

import pytest
from kun.governance.diagnosis_scope import MAX_SCOPE_MODULES, narrow_scope
from kun.governance.rcdh import (
    REPEAT_FORCE_ESCALATION_THRESHOLD,
    DiagnosticRecord,
    LevelCheckResult,
    run_diagnostic,
)

# ---- narrow_scope ----


@pytest.mark.asyncio
async def test_narrow_scope_empty() -> None:
    out = await narrow_scope("a mysterious problem", evidence=None)
    assert out == []


@pytest.mark.asyncio
async def test_narrow_scope_picks_explicit_module_in_evidence() -> None:
    out = await narrow_scope(
        "task failed",
        evidence=[
            {"module": "kun/agents/executor", "kind": "stack_trace"},
            {"module": "kun/agents/executor", "kind": "log"},
        ],
    )
    assert "kun/agents/executor" in out


@pytest.mark.asyncio
async def test_narrow_scope_extracts_path_from_symptom() -> None:
    out = await narrow_scope(
        "AttributeError in kun/interface/llm/router invoke",
        evidence=None,
    )
    assert any("kun/interface/llm/router" in m for m in out)


@pytest.mark.asyncio
async def test_narrow_scope_matches_dotted_path() -> None:
    out = await narrow_scope(
        "error originated in kun.agents.supervisor.service",
        evidence=None,
    )
    # 应该归一为 kun/agents/supervisor/service
    assert any("kun/agents/supervisor/service" in m for m in out)


@pytest.mark.asyncio
async def test_narrow_scope_keyword_known_module() -> None:
    out = await narrow_scope("the executor sometimes loops")
    assert any("executor" in m for m in out)


@pytest.mark.asyncio
async def test_narrow_scope_caps_at_5() -> None:
    evidence = [{"module": f"kun/m_{i}"} for i in range(10)]
    out = await narrow_scope("multi-module", evidence=evidence)
    assert len(out) <= MAX_SCOPE_MODULES


@pytest.mark.asyncio
async def test_narrow_scope_evidence_outweighs_keyword() -> None:
    """显式 evidence module 排在 keyword 命中之前."""
    out = await narrow_scope(
        "the executor failed",
        evidence=[{"module": "kun/agents/strategist", "kind": "log"}],
    )
    # strategist (evidence, weight 10) 应该在 executor (keyword, weight 1) 前
    assert out[0] == "kun/agents/strategist"


# ---- run_diagnostic L0 design ----


@pytest.mark.asyncio
async def test_l0_design_keyword_hits() -> None:
    record = await run_diagnostic(
        "The ADR specifies X but the policy says Y, contract ambiguous.",
        triggered_by_event_id="e1",
    )
    assert isinstance(record, DiagnosticRecord)
    assert record.root_cause_level == 0
    assert record.recommended_action == "redesign"
    assert record.level_0_check.is_root_cause is True


@pytest.mark.asyncio
async def test_l0_cross_module_impact() -> None:
    record = await run_diagnostic(
        "failure observed",
        triggered_by_event_id="e1",
        evidence=[
            {"module": "kun/a"},
            {"module": "kun/b"},
            {"module": "kun/c"},
        ],
    )
    assert record.root_cause_level == 0
    assert any(
        ev.get("type") == "cross_module_impact"
        for ev in record.level_0_check.evidence
    )


# ---- L1 activation ----


@pytest.mark.asyncio
async def test_l1_keyword_not_wired() -> None:
    record = await run_diagnostic(
        "capability not active for the supervisor",
        triggered_by_event_id="e1",
    )
    # "supervisor" 是 known module → also narrow_scope hit, 但 L1 keyword 优先
    # L0 关键词没命中, L1 命中 → root cause L1
    assert record.root_cause_level == 1
    assert record.recommended_action == "activate"


@pytest.mark.asyncio
async def test_l1_capability_state_disabled() -> None:
    record = await run_diagnostic(
        "task failed",
        triggered_by_event_id="e1",
        evidence=[
            {"capability": "cap_foo", "module": "kun/x"},
        ],
        capability_state={"cap_foo": False},
    )
    assert record.root_cause_level == 1
    cap_hits = [
        ev for ev in record.level_1_check.evidence
        if ev.get("type") == "capability_disabled"
    ]
    assert len(cap_hits) == 1
    assert cap_hits[0]["capability"] == "cap_foo"


@pytest.mark.asyncio
async def test_l1_skipped_when_capability_enabled() -> None:
    record = await run_diagnostic(
        "intermittent failure",
        triggered_by_event_id="e1",
        evidence=[{"capability": "cap_foo", "module": "kun/x"}],
        capability_state={"cap_foo": True},
    )
    # L1 不命中 → 继续往下 (L2 module focus 命中 single module)
    assert record.level_1_check.is_root_cause is False


# ---- L2 module ----


@pytest.mark.asyncio
async def test_l2_single_module_focus() -> None:
    record = await run_diagnostic(
        "module-specific bug",
        triggered_by_event_id="e1",
        scope_modules=["kun/agents/executor"],
    )
    assert record.root_cause_level == 2
    assert record.recommended_action == "module_rsi"


@pytest.mark.asyncio
async def test_l2_repeat_module_in_evidence() -> None:
    record = await run_diagnostic(
        "no specific signal",
        triggered_by_event_id="e1",
        evidence=[
            {"module": "kun/foo", "kind": "log"},
            {"module": "kun/foo", "kind": "log"},
        ],
    )
    assert record.root_cause_level == 2
    hits = [
        ev for ev in record.level_2_check.evidence
        if ev.get("type") == "module_repeat_in_evidence"
    ]
    assert hits[0]["module"] == "kun/foo"


# ---- L3 code ----


@pytest.mark.asyncio
async def test_l3_stack_trace_in_symptom() -> None:
    record = await run_diagnostic(
        'Traceback (most recent call last):\n  File "x.py", line 42, in foo',
        triggered_by_event_id="e1",
    )
    # symptom 同时含 "specification" 可能没含, 应纯 L3
    assert record.root_cause_level == 3
    assert record.recommended_action == "code_fix"


@pytest.mark.asyncio
async def test_l3_file_line_in_evidence() -> None:
    record = await run_diagnostic(
        "unspecific failure",
        triggered_by_event_id="e1",
        evidence=[{"file": "x.py", "line": 100, "kind": "log"}],
    )
    assert record.root_cause_level == 3


# ---- 强制升级 ----


@pytest.mark.asyncio
async def test_force_escalation_to_l0_after_n_repeats() -> None:
    record = await run_diagnostic(
        "the same module-specific bug again",
        triggered_by_event_id="e1",
        scope_modules=["kun/foo"],
        repeat_history_count=REPEAT_FORCE_ESCALATION_THRESHOLD,
    )
    assert record.root_cause_level == 0
    assert record.recommended_action == "redesign"
    # 原本是 L2 — 强制 escalation evidence 落 L0
    assert any(
        ev.get("type") == "force_escalation"
        for ev in record.level_0_check.evidence
    )


@pytest.mark.asyncio
async def test_force_escalation_does_not_demote_l0_or_l1() -> None:
    """已经定 L1 + 重复 ≥ 3 次 → 保持 L1 (强制升级只升 L2/L3 → L0)."""
    record = await run_diagnostic(
        "feature flag disabled",
        triggered_by_event_id="e1",
        repeat_history_count=5,
    )
    # L1 hit, 不被 force escalation 改
    assert record.root_cause_level == 1


@pytest.mark.asyncio
async def test_no_force_escalation_below_threshold() -> None:
    record = await run_diagnostic(
        "module bug",
        triggered_by_event_id="e1",
        scope_modules=["kun/foo"],
        repeat_history_count=2,  # 未到 3
    )
    assert record.root_cause_level == 2


# ---- scope_modules 上限 ----


@pytest.mark.asyncio
async def test_scope_modules_over_limit_rejected() -> None:
    with pytest.raises(ValueError, match="≤"):
        await run_diagnostic(
            "x",
            triggered_by_event_id="e1",
            scope_modules=["m1", "m2", "m3", "m4", "m5", "m6"],
        )


@pytest.mark.asyncio
async def test_auto_narrow_when_no_scope_provided() -> None:
    record = await run_diagnostic(
        "error in kun/agents/executor handler",
        triggered_by_event_id="e1",
    )
    # narrow_scope 应该圈定 kun/agents/executor
    assert any("executor" in m for m in record.scope_modules)


# ---- 无任何信号 ----


@pytest.mark.asyncio
async def test_no_root_cause_when_no_signals() -> None:
    record = await run_diagnostic(
        "x", triggered_by_event_id="e1"
    )
    assert record.root_cause_level is None
    assert record.recommended_action is None


# ---- LevelCheckResult sanity ----


def test_level_check_result_defaults() -> None:
    r = LevelCheckResult()
    assert r.skipped is False
    assert r.is_root_cause is False
    assert r.evidence == []
