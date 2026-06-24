"""L3.5 — 自指限制强化 单测.

  - kun/governance/self_referential.py 集中 source of truth
  - Strategist 自指 → 强制 target_level=0
  - Gate 独立 target_module 检查 (不依赖 caller 标 flag)
  - Gate enable_capability 自指 capability 强 gate (要 human_approval_token)
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.gate.service import GateService
from kun.agents.strategist.service import StrategistService
from kun.governance.self_referential import (
    SELF_REFERENTIAL_PREFIXES,
    is_self_referential,
)

# ---- shared module ----


def test_prefixes_complete() -> None:
    assert SELF_REFERENTIAL_PREFIXES == (
        "strategist",
        "supervisor",
        "gate",
        "director",
        "external_supervisor",
    )


def test_is_self_referential_none_returns_false() -> None:
    assert is_self_referential(None) is False
    assert is_self_referential("") is False


def test_is_self_referential_naming_forms() -> None:
    assert is_self_referential("strategist")
    assert is_self_referential("supervisor.service")
    assert is_self_referential("kun.agents.gate")
    assert is_self_referential("kun/agents/director/intent")
    assert is_self_referential("external_supervisor.modes")


def test_is_self_referential_rejects_other() -> None:
    assert is_self_referential("executor.tool_calling") is False
    assert is_self_referential("llm.router") is False
    assert is_self_referential("skill.bug_fix") is False


# ---- Strategist 自指强化 ----


@pytest.mark.asyncio
async def test_strategist_self_referential_forces_target_level_0() -> None:
    """L3.5: 自指候选 target_level 强制升到 0 (设计层)."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "strategist.service",
            "evidence": [{"task_type": "x"}],
        }
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.requires_human_review is True
    assert c.target_level == 0  # L3.5 强制
    assert "forced target_level=0 design-level" in c.rationale


@pytest.mark.asyncio
async def test_strategist_non_self_referential_keeps_original_level() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "executor.coding",
            "evidence": [{"task_type": "x"}],
        }
    )
    c = candidates[0]
    # task_failure_spike 默认 target_level=2 (模块层)
    assert c.target_level == 2
    assert c.requires_human_review is False


# ---- Gate 独立 target_module 检查 ----


def _ok_test_report() -> dict[str, Any]:
    return {"pass_rate": 0.95, "passed_count": 19, "total_count": 20}


def _ok_debrief() -> dict[str, Any]:
    return {
        "verdict": "ok",
        "rationale": "anchor satisfied",
        "evidence_quality_score": 0.8,
    }


@pytest.mark.asyncio
async def test_gate_detects_self_referential_target_even_without_flag() -> None:
    """L3.5: Gate 独立检查 target_module — caller 漏标 requires_human_review 也命中."""
    svc = GateService()
    experiment = {
        "experiment_id": "er-1",
        "target_module": "supervisor.service",  # 自指, 但 caller 漏标 flag
        "rationale": "x",
        "change_spec": {"kind": "test"},
        # 注意: 没有 requires_human_review 字段
    }
    decision = await svc.admit(
        experiment, test_report=_ok_test_report(), debrief=_ok_debrief()
    )
    # L3.5: target_module 命中 → awaiting_human_review
    assert decision.verdict == "awaiting_human_review"
    assert decision.rule_results["R4_self_referential"] is False
    assert any("self_referential_target_module" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_gate_writes_capability_row_for_self_referential() -> None:
    """L3.5: 自指 awaiting_human_review 也写一条 capability row (promotion_state=awaiting)."""
    written: list[dict] = []

    async def fake_writer(payload: dict[str, Any]) -> None:
        written.append(payload)

    svc = GateService(capability_writer=fake_writer)
    experiment = {
        "experiment_id": "er-1",
        "target_module": "director.intent",
        "rationale": "force-rewrite intent inference",
        "change_spec": {"kind": "rewrite"},
    }
    decision = await svc.admit(
        experiment, test_report=_ok_test_report(), debrief=_ok_debrief()
    )
    assert decision.verdict == "awaiting_human_review"
    assert decision.capability_id is not None
    assert decision.capability_row_payload is not None
    assert len(written) == 1
    row = written[0]
    assert row["promotion_state"] == "awaiting_human_review"
    assert row["enabled"] is False
    assert row["sampling_rate"] == 0.0  # 不 sample 任何流量
    assert row["metadata"]["promotion_block_self_referential"] is True


# ---- Gate enable_capability 自指 gate ----


@pytest.mark.asyncio
async def test_enable_capability_blocked_for_self_referential_without_token() -> None:
    async def metadata_lookup(cap_id: str) -> dict[str, Any]:
        return {"promotion_block_self_referential": True}

    svc = GateService()
    with pytest.raises(PermissionError, match="self-referential"):
        await svc.enable_capability(
            "cp-1", metadata_lookup=metadata_lookup
        )


@pytest.mark.asyncio
async def test_enable_capability_allowed_with_human_approval_token() -> None:
    async def metadata_lookup(cap_id: str) -> dict[str, Any]:
        return {"promotion_block_self_referential": True}

    svc = GateService()
    # 不抛, 走通
    payload = await svc.enable_capability(
        "cp-1",
        metadata_lookup=metadata_lookup,
        human_approval_token="hkv-token-xyz",
    )
    assert payload["enabled"] is True


@pytest.mark.asyncio
async def test_enable_capability_no_block_for_non_self_referential() -> None:
    async def metadata_lookup(cap_id: str) -> dict[str, Any]:
        return {"promotion_block_self_referential": False}

    svc = GateService()
    payload = await svc.enable_capability(
        "cp-1", metadata_lookup=metadata_lookup
    )
    assert payload["enabled"] is True


@pytest.mark.asyncio
async def test_enable_capability_no_metadata_lookup_passes_through() -> None:
    """无 metadata_lookup 注入 → 不做自指 gate (backward-compat)."""
    svc = GateService()
    payload = await svc.enable_capability("cp-1")
    assert payload["enabled"] is True


# ---- 集成: Strategist 自指候选经 Gate ----


@pytest.mark.asyncio
async def test_full_flow_strategist_self_referential_to_gate() -> None:
    """Strategist 自指 → Gate 检测到 requires_human_review=True → awaiting."""
    strat = StrategistService()
    candidates = await strat.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "gate.service",
            "evidence": [{"task_type": "x"}],
        }
    )
    self_ref_candidate = candidates[0]
    # 转 Gate-consumable dict
    experiment_dict = {
        "experiment_id": self_ref_candidate.experiment_id,
        "target_module": self_ref_candidate.target_module,
        "rationale": self_ref_candidate.rationale,
        "change_spec": self_ref_candidate.change_spec,
        "requires_human_review": self_ref_candidate.requires_human_review,
        "rollback_on": self_ref_candidate.rollback_on,
    }
    gate = GateService()
    decision = await gate.admit(
        experiment_dict,
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    assert decision.verdict == "awaiting_human_review"
    assert decision.capability_row_payload is not None
    assert decision.capability_row_payload["metadata"]["promotion_block_self_referential"] is True
