"""L2.8 — GateService 准入门禁单测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.gate.service import GateDecision, GateService


def _ok_test_report() -> dict[str, Any]:
    return {"pass_rate": 0.95, "passed_count": 19, "total_count": 20}


def _ok_debrief() -> dict[str, Any]:
    return {
        "verdict": "ok",
        "rationale": "anchor satisfied",
        "evidence_quality_score": 0.8,
    }


def _ok_experiment() -> dict[str, Any]:
    return {
        "experiment_id": "er-1",
        "target_module": "llm.router",
        "change_spec": {"kind": "tier_upgrade"},
        "rationale": "promote tier",
        "explorer_mode": "conservative",
        "sampling_rate": 0.3,
        "rollback_on": [],
    }


# ---- approve happy path ----


@pytest.mark.asyncio
async def test_admit_approves_when_all_checks_pass() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
        tenant_id="u-1",
    )
    assert isinstance(decision, GateDecision)
    assert decision.verdict == "approve"
    assert decision.capability_id is not None
    assert decision.promotion_state == "merged"
    row = decision.capability_row_payload
    assert row is not None
    assert row["tenant_id"] == "u-1"
    assert row["enabled"] is False
    assert row["target_module"] == "llm.router"
    assert row["promotion_state"] == "merged"
    assert row["metadata"]["explorer_mode"] == "conservative"
    assert row["metadata"]["test_pass_rate"] == 0.95
    assert row["metadata"]["debrief_verdict"] == "ok"


# ---- R1 test_report ----


@pytest.mark.asyncio
async def test_admit_rejects_missing_test_report() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(), test_report=None
    )
    assert decision.verdict == "reject"
    assert any("missing_test_report" in r for r in decision.reasons)
    assert decision.rule_results["R1_test_report"] is False


@pytest.mark.asyncio
async def test_admit_rejects_low_pass_rate() -> None:
    svc = GateService(min_pass_rate=0.9)
    decision = await svc.admit(
        _ok_experiment(),
        test_report={"pass_rate": 0.7, "passed_count": 7, "total_count": 10},
    )
    assert decision.verdict == "reject"
    assert any("test_pass_rate=0.70" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_admit_computes_pass_rate_from_counts() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report={"passed_count": 95, "total_count": 100},
        debrief=_ok_debrief(),
    )
    assert decision.verdict == "approve"


# ---- R2 diagnostic ----


@pytest.mark.asyncio
async def test_admit_requires_diagnostic_for_fix() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
        is_fix=True,
        diagnostic_record=None,
    )
    assert decision.verdict == "reject"
    assert any("fix_requires_diagnostic_record" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_admit_accepts_fix_with_diagnostic() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
        is_fix=True,
        diagnostic_record={"diagnostic_id": "dx-1", "root_cause_level": 2},
    )
    assert decision.verdict == "approve"
    assert decision.capability_row_payload is not None
    assert decision.capability_row_payload["metadata"]["diagnostic_id"] == "dx-1"


@pytest.mark.asyncio
async def test_admit_rejects_diagnostic_without_id() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        is_fix=True,
        diagnostic_record={"root_cause_level": 2},  # no id
    )
    assert decision.verdict == "reject"
    assert any("missing_id" in r for r in decision.reasons)


# ---- R3 debrief ----


@pytest.mark.asyncio
async def test_admit_rejects_alarming_debrief() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief={"verdict": "alarming", "rationale": "fake tests"},
    )
    assert decision.verdict == "reject"
    assert any("debrief_verdict=alarming" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_admit_rejects_low_evidence_quality() -> None:
    svc = GateService(min_evidence_quality=0.6)
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief={"verdict": "ok", "evidence_quality_score": 0.3},
    )
    assert decision.verdict == "reject"
    assert any("evidence_quality_score=0.30" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_admit_accepts_concerning_but_quality_ok() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief={"verdict": "concerning", "evidence_quality_score": 0.7},
    )
    # concerning 不直接 reject (只 alarming)
    assert decision.verdict == "approve"


@pytest.mark.asyncio
async def test_admit_no_debrief_allowed() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=None,
    )
    assert decision.verdict == "approve"


# ---- R4 self-referential ----


@pytest.mark.asyncio
async def test_admit_self_referential_marked_for_human_review() -> None:
    svc = GateService()
    exp = _ok_experiment()
    exp["requires_human_review"] = True
    decision = await svc.admit(
        exp,
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    assert decision.verdict == "awaiting_human_review"
    assert decision.promotion_state == "awaiting_human_review"
    assert decision.capability_row_payload is None
    assert decision.rule_results["R4_self_referential"] is False


# ---- writer ----


@pytest.mark.asyncio
async def test_admit_calls_capability_writer_on_approve() -> None:
    written: list[dict] = []

    async def fake_writer(payload: dict[str, Any]) -> None:
        written.append(payload)

    svc = GateService(capability_writer=fake_writer)
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    assert decision.verdict == "approve"
    assert len(written) == 1
    assert written[0]["capability_id"] == decision.capability_id


@pytest.mark.asyncio
async def test_admit_writer_not_called_on_reject() -> None:
    written: list[dict] = []

    async def fake_writer(payload: dict[str, Any]) -> None:
        written.append(payload)

    svc = GateService(capability_writer=fake_writer)
    decision = await svc.admit(_ok_experiment(), test_report=None)
    assert decision.verdict == "reject"
    assert written == []


@pytest.mark.asyncio
async def test_admit_writer_exception_does_not_propagate() -> None:
    async def bad_writer(payload: dict[str, Any]) -> None:
        raise RuntimeError("DB down")

    svc = GateService(capability_writer=bad_writer)
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    # 仍返回 approve, 异常吞
    assert decision.verdict == "approve"


# ---- enable_capability ----


@pytest.mark.asyncio
async def test_enable_capability_flips_enabled() -> None:
    svc = GateService()
    payload = await svc.enable_capability("cp-1")
    assert payload["enabled"] is True
    assert payload["promotion_state"] == "enabled"
    assert payload["capability_id"] == "cp-1"


@pytest.mark.asyncio
async def test_enable_capability_uses_writer() -> None:
    written: list[dict] = []

    async def fake_writer(payload: dict[str, Any]) -> None:
        written.append(payload)

    svc = GateService()
    await svc.enable_capability(
        "cp-1", tenant_id="u-test", capability_state_writer=fake_writer
    )
    assert len(written) == 1
    assert written[0]["enabled"] is True


@pytest.mark.asyncio
async def test_enable_capability_writer_exception_propagates() -> None:
    """enable_capability 写库失败要让 caller 知道 — 不能静默成功 fake."""

    async def bad_writer(payload: dict[str, Any]) -> None:
        raise RuntimeError("DB down")

    svc = GateService()
    with pytest.raises(RuntimeError):
        await svc.enable_capability(
            "cp-1", capability_state_writer=bad_writer
        )


# ---- promotion_deadline ----


@pytest.mark.asyncio
async def test_promotion_deadline_default_14_days() -> None:
    svc = GateService()
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    row = decision.capability_row_payload
    assert row is not None
    delta = row["promotion_deadline"] - row["promotion_started_at"]
    assert delta.days == 14


@pytest.mark.asyncio
async def test_promotion_deadline_configurable() -> None:
    svc = GateService(promotion_deadline_days=7)
    decision = await svc.admit(
        _ok_experiment(),
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    row = decision.capability_row_payload
    assert row is not None
    delta = row["promotion_deadline"] - row["promotion_started_at"]
    assert delta.days == 7
