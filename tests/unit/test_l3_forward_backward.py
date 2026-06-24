"""L3.3 — Forward / Backward 双修复策略 单测.

Strategist auto-select: 最近 24h 有 enabled capability 命中 target_module → backward
否则 → forward (Explorer Pool 默认).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.agents.strategist.service import (
    StrategistService,
    StrategyExperiment,
    _candidate_for_backward_rollback,
    select_repair_direction,
)

# ---- select_repair_direction ----


def test_direction_forward_when_no_history() -> None:
    out = select_repair_direction({"target_module": "x"}, None)
    assert out == "forward"


def test_direction_forward_when_empty_history() -> None:
    out = select_repair_direction({"target_module": "x"}, [])
    assert out == "forward"


def test_direction_forward_when_history_only_disabled() -> None:
    """history 有 entry 但 enabled=False → forward."""
    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=2),
        "enabled": False,
        "change_summary": "old change",
    }
    out = select_repair_direction({"target_module": "x"}, [cap])
    assert out == "forward"


def test_direction_backward_when_recent_enabled_capability() -> None:
    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=3),
        "enabled": True,
        "change_summary": "tier upgrade",
    }
    out = select_repair_direction({"target_module": "x"}, [cap])
    assert out == "backward"


def test_direction_forward_when_capability_too_old() -> None:
    """24h 以外的 capability → forward (无关嫌疑)."""
    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=48),
        "enabled": True,
        "change_summary": "old change",
    }
    out = select_repair_direction({"target_module": "x"}, [cap])
    assert out == "forward"


def test_direction_accepts_iso_string_timestamps() -> None:
    """promoted_at 字符串 ISO 也接受."""
    recent_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    cap = {
        "capability_id": "cp-1",
        "promoted_at": recent_ts,
        "enabled": True,
    }
    assert select_repair_direction({"target_module": "x"}, [cap]) == "backward"


def test_direction_iso_without_tzinfo_treated_as_utc() -> None:
    """no tzinfo ISO 字符串当 UTC."""
    ts = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    cap = {"capability_id": "cp-1", "promoted_at": ts, "enabled": True}
    assert select_repair_direction({"target_module": "x"}, [cap]) == "backward"


# ---- _candidate_for_backward_rollback ----


def test_backward_candidate_shape() -> None:
    cap = {
        "capability_id": "cp-1",
        "change_summary": "promoted tier_upgrade for executor.coding",
        "enabled": True,
    }
    exp = _candidate_for_backward_rollback(
        {"target_module": "kun.agents.executor", "evidence": [{"type": "anomaly_rate"}]},
        cap,
    )
    assert isinstance(exp, StrategyExperiment)
    assert exp.explorer_mode == "backward"
    assert exp.rollout_mode == "direct"
    assert exp.change_spec["kind"] == "capability_rollback"
    assert exp.change_spec["rollback_capability_id"] == "cp-1"
    assert exp.change_spec["direction"] == "backward"
    assert "promoted tier_upgrade for executor.coding" in exp.change_spec["original_change_summary"]
    # rollback_on 必须含"回滚的回滚"保护
    rb_metrics = {r["metric"] for r in exp.rollback_on}
    assert "anomaly_rate" in rb_metrics


# ---- propose_candidates routing ----


@pytest.mark.asyncio
async def test_propose_uses_backward_when_recent_capability() -> None:
    """history reader 报告最近有 enabled cap → 返回 backward 单候选."""
    cap = {
        "capability_id": "cp-recent",
        "promoted_at": datetime.now(UTC) - timedelta(hours=1),
        "enabled": True,
        "change_summary": "tier upgraded last hour",
    }

    async def history_reader(target_module: str) -> list[dict]:
        assert target_module == "llm.router"
        return [cap]

    svc = StrategistService(capability_history_reader=history_reader)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 1
    assert candidates[0].explorer_mode == "backward"
    assert candidates[0].change_spec["rollback_capability_id"] == "cp-recent"


@pytest.mark.asyncio
async def test_propose_uses_forward_when_no_history_reader() -> None:
    """无 history_reader → forward Explorer Pool 默认行为."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 3
    modes = {c.explorer_mode for c in candidates}
    assert "backward" not in modes
    assert modes == {"conservative", "aggressive", "performance"}


@pytest.mark.asyncio
async def test_propose_uses_forward_when_history_empty() -> None:
    async def empty_reader(target_module: str) -> list[dict]:
        return []

    svc = StrategistService(capability_history_reader=empty_reader)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 3
    assert {c.explorer_mode for c in candidates} == {
        "conservative",
        "aggressive",
        "performance",
    }


@pytest.mark.asyncio
async def test_propose_history_reader_exception_falls_back_to_forward() -> None:
    """history_reader 抛异常 → 回退 forward, 不打挂 Strategist."""

    async def bad_reader(target_module: str) -> list[dict]:
        raise RuntimeError("DB unavailable")

    svc = StrategistService(capability_history_reader=bad_reader)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 3
    assert {c.explorer_mode for c in candidates} == {
        "conservative",
        "aggressive",
        "performance",
    }


@pytest.mark.asyncio
async def test_propose_backward_emitter_called() -> None:
    """backward candidate 也走 emitter (落库)."""
    received: list[StrategyExperiment] = []

    async def fake_emitter(exp: StrategyExperiment) -> None:
        received.append(exp)

    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=2),
        "enabled": True,
        "change_summary": "x",
    }

    async def history_reader(target_module: str) -> list[dict]:
        return [cap]

    svc = StrategistService(
        emitter=fake_emitter, capability_history_reader=history_reader
    )
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "executor.x",
            "evidence": [{"task_type": "x"}],
        }
    )
    assert len(candidates) == 1
    assert len(received) == 1
    assert received[0].explorer_mode == "backward"


@pytest.mark.asyncio
async def test_propose_backward_skips_self_referential_check_via_emit_helper() -> None:
    """backward 候选 target_module 命中监督角色也应被标 human review."""
    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=2),
        "enabled": True,
        "change_summary": "x",
    }

    async def history_reader(target_module: str) -> list[dict]:
        return [cap]

    svc = StrategistService(capability_history_reader=history_reader)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "task_failure_spike",
            "target_module": "strategist.service",  # 自指!
            "evidence": [{"task_type": "x"}],
        }
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.explorer_mode == "backward"
    # 同时是 self-referential → 必须人审
    assert c.requires_human_review is True
    assert c.status == "awaiting_human_review"
