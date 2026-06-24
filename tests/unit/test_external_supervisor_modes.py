"""L2.5 — External Supervisor 三 Mode 单测.

Mode A (gate_review) / Mode B (task_debrief) / 自嗨检测.
"""

from __future__ import annotations

import json

import pytest
from kun.external_supervisor.modes import (
    DebriefRecord,
    GateAdvisory,
    SelfAggrandizementCheck,
    _compute_evidence_quality_score,
    _detect_self_aggrandizement_signals,
    check_self_aggrandizement,
    mode_a_gate_review,
    mode_b_task_debrief,
)
from kun.external_supervisor.service import ExternalSupervisorService
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.stub_provider import StubProvider


def _service_returning(content: str) -> ExternalSupervisorService:
    """ExternalSupervisorService wired to a stub that returns fixed content."""

    def builder(request):
        return LLMResponse(
            content=content,
            usage=UsageInfo(input_tokens=10, output_tokens=20),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    stub = StubProvider(model_id="stub-1", tier="cheap", latency_ms=0, builder=builder)
    return ExternalSupervisorService(llm_provider=stub)


# ---- Mode A · gate_review ----


@pytest.mark.asyncio
async def test_mode_a_approve_path() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "ok", "rationale": "all checks clean"})
    )
    advisory = await mode_a_gate_review(
        svc,
        anchor={"goal_statement": "ship auth"},
        gate_evidence={"test_report": {"passed": 12, "failed": 0}},
    )
    assert isinstance(advisory, GateAdvisory)
    assert advisory.verdict == "approve"
    assert advisory.recommended_action == "continue"
    assert advisory.rationale == "all checks clean"


@pytest.mark.asyncio
async def test_mode_a_escalate_on_concerning() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "concerning", "rationale": "marginal coverage"})
    )
    advisory = await mode_a_gate_review(
        svc, anchor=None, gate_evidence={"x": 1}
    )
    assert advisory.verdict == "escalate"
    assert advisory.recommended_action == "pause"


@pytest.mark.asyncio
async def test_mode_a_block_on_alarming() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "alarming", "rationale": "fabricated test report"})
    )
    advisory = await mode_a_gate_review(
        svc, anchor=None, gate_evidence={}
    )
    assert advisory.verdict == "block"
    assert advisory.recommended_action == "human_review"


@pytest.mark.asyncio
async def test_mode_a_respects_llm_recommended_action() -> None:
    svc = _service_returning(
        json.dumps(
            {
                "verdict": "concerning",
                "rationale": "x",
                "recommended_action": "request_more_evidence",
            }
        )
    )
    advisory = await mode_a_gate_review(
        svc, anchor=None, gate_evidence={}
    )
    # LLM 提供的 recommended_action 优先于默认 mapping
    assert advisory.recommended_action == "request_more_evidence"


# ---- Mode B · task_debrief ----


def test_evidence_quality_score_zero_for_empty() -> None:
    assert _compute_evidence_quality_score([], {}) == 0.0


def test_evidence_quality_score_rewards_test_report() -> None:
    artifacts = [{"kind": "test_report"}]
    score = _compute_evidence_quality_score(artifacts, {})
    assert score > 0.3


def test_evidence_quality_score_caps_at_one() -> None:
    artifacts = [{"kind": k, "path": f"/p/{i}"} for i, k in enumerate(
        ["test_report", "artifact_link", "decision", "debrief", "diagnostic"]
    )]
    score = _compute_evidence_quality_score(artifacts, {})
    assert 0.0 <= score <= 1.0


@pytest.mark.asyncio
async def test_mode_b_writes_quality_score_into_writeback() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "ok", "rationale": "anchor satisfied"})
    )
    debrief = await mode_b_task_debrief(
        svc,
        anchor={"goal_statement": "x"},
        task_summary={"task_id": "t1", "criteria_done_count": 3, "criteria_total_count": 3},
        artifacts=[
            {"kind": "test_report", "path": "/reports/r1.json"},
            {"kind": "artifact_link", "path": "/out.zip"},
        ],
        target_task_id="t1",
        target_anchor_id="ga1",
    )
    assert isinstance(debrief, DebriefRecord)
    assert debrief.verdict == "ok"
    assert 0.5 < debrief.evidence_quality_score <= 1.0
    wb = debrief.recommended_capability_writeback
    assert wb["supervisor_verdict"] == "ok"
    assert wb["evidence_quality_score"] == debrief.evidence_quality_score


@pytest.mark.asyncio
async def test_mode_b_alarming_includes_promotion_block_reason() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "alarming", "rationale": "no real tests run"})
    )
    debrief = await mode_b_task_debrief(
        svc, anchor=None, task_summary={}, artifacts=[]
    )
    wb = debrief.recommended_capability_writeback
    assert "promotion_block_reason" in wb
    assert "no real tests" in wb["promotion_block_reason"]
    assert debrief.evidence_quality_score == 0.0


# ---- 自嗨检测 ----


def test_detect_signals_claims_done_no_test_report() -> None:
    signals = _detect_self_aggrandizement_signals(
        {"claims_complete": True},
        [{"kind": "artifact_link"}],
    )
    assert "claims_all_done_but_no_test_report" in signals


def test_detect_signals_evidence_count_zero() -> None:
    signals = _detect_self_aggrandizement_signals(
        {"claims_complete": True}, []
    )
    assert "evidence_count_zero" in signals


def test_detect_signals_fallback_lie() -> None:
    signals = _detect_self_aggrandizement_signals(
        {"fallback_triggered": False, "claims_complete": True},
        [
            {"kind": "test_report"},
            {"kind": "llm_fallback_record", "provider": "minimax"},
        ],
    )
    assert "fallback_triggered_but_self_report_clean" in signals


def test_detect_signals_short_rationale_for_complex_task() -> None:
    signals = _detect_self_aggrandizement_signals(
        {"rationale": "done", "estimated_steps": 8},
        [{"kind": "test_report"}],
    )
    assert "rationale_too_short_vs_complexity" in signals


def test_detect_signals_unverified_paths() -> None:
    signals = _detect_self_aggrandizement_signals(
        {"cited_paths": ["/real.py", "/imaginary.py"]},
        [{"kind": "artifact_link", "path": "/real.py"}],
    )
    assert any(s.startswith("unverified_path:/imaginary.py") for s in signals)
    assert not any(s.startswith("unverified_path:/real.py") for s in signals)


def test_detect_signals_clean_self_report_no_signals() -> None:
    signals = _detect_self_aggrandizement_signals(
        {
            "criteria_done_count": 2,
            "criteria_total_count": 3,  # not yet done
            "rationale": "still working on the third criterion",
            "estimated_steps": 3,
        },
        [{"kind": "test_report", "path": "/r.json"}],
    )
    assert signals == []


@pytest.mark.asyncio
async def test_self_aggrandizement_skip_llm_when_no_signals() -> None:
    """工程化 0 signal + always_call_llm=False → 不调 LLM."""

    invocations: list[bool] = []

    def builder(request):
        invocations.append(True)
        return LLMResponse(
            content='{"verdict": "alarming", "rationale": "should not be called"}',
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    svc = ExternalSupervisorService(
        llm_provider=StubProvider(model_id="stub-1", tier="cheap", latency_ms=0, builder=builder)
    )
    result = await check_self_aggrandizement(
        svc,
        anchor=None,
        executor_self_report={"criteria_done_count": 1, "criteria_total_count": 3},
        evidence_artifacts=[{"kind": "test_report", "path": "/r.json"}],
    )
    assert isinstance(result, SelfAggrandizementCheck)
    assert result.is_self_aggrandizing is False
    assert invocations == []  # LLM 真没调


@pytest.mark.asyncio
async def test_self_aggrandizement_calls_llm_when_signal_hit() -> None:
    svc = _service_returning(
        json.dumps({"verdict": "alarming", "rationale": "no tests, claims complete"})
    )
    result = await check_self_aggrandizement(
        svc,
        anchor={"goal_statement": "ship feature X"},
        executor_self_report={"claims_complete": True},
        evidence_artifacts=[],  # 触发 evidence_count_zero + claims_all_done_but_no_test_report
    )
    assert result.is_self_aggrandizing is True
    assert "claims_all_done_but_no_test_report" in result.engineering_signals
    assert "evidence_count_zero" in result.engineering_signals
    assert result.llm_verdict == "alarming"
    assert result.underlying is not None


@pytest.mark.asyncio
async def test_self_aggrandizement_always_call_llm_flag() -> None:
    """always_call_llm=True 即使无信号也调 LLM."""

    captured: list = []

    def builder(request):
        captured.append(request)
        return LLMResponse(
            content='{"verdict": "ok", "rationale": "looks fine"}',
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            model="stub-1",
            provider="stub",
            tier="cheap",
            finish_reason="stop",
        )

    svc = ExternalSupervisorService(
        llm_provider=StubProvider(model_id="stub-1", tier="cheap", latency_ms=0, builder=builder)
    )
    result = await check_self_aggrandizement(
        svc,
        anchor=None,
        executor_self_report={"criteria_done_count": 1, "criteria_total_count": 3},
        evidence_artifacts=[{"kind": "test_report"}],
        always_call_llm=True,
    )
    assert len(captured) == 1  # LLM 被调
    assert result.is_self_aggrandizing is False  # 0 信号 + ok verdict
    assert result.llm_verdict == "ok"


@pytest.mark.asyncio
async def test_self_aggrandizement_single_signal_not_enough_for_flag() -> None:
    """1 工程化 signal + LLM ok → 不判自嗨 (single signal noisy)."""
    svc = _service_returning(
        json.dumps({"verdict": "ok", "rationale": "minor; not self-aggrandizing"})
    )
    result = await check_self_aggrandizement(
        svc,
        anchor=None,
        executor_self_report={
            "claims_complete": True,
            "rationale": "extensive analysis here lorem ipsum dolor sit amet consectetur",
            "estimated_steps": 1,
        },
        # 1 信号 only: claims_all_done_but_no_test_report (artifact 有但非 test_report)
        evidence_artifacts=[{"kind": "artifact_link", "path": "/x.zip"}],
    )
    assert len(result.engineering_signals) == 1
    assert result.is_self_aggrandizing is False
    assert result.llm_verdict == "ok"
