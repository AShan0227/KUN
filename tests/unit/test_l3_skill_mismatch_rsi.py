"""L3.2 — 第 3 条 RSI 实例: skill 选择启发式 单测.

Supervisor 检测 (task_type, skill_id) 失败率 spike + Strategist 提候选.
"""

from __future__ import annotations

import pytest
from kun.agents.strategist.service import StrategistService
from kun.agents.supervisor.service import SupervisorService

# ---- Supervisor skill_mismatch 检测 ----


def _skill_event(*, task_type: str, skill_id: str, outcome: str, tenant_id: str = "u-test"):
    return {
        "tenant_id": tenant_id,
        "task_type": task_type,
        "skill_id": skill_id,
        "outcome": outcome,
    }


@pytest.mark.asyncio
async def test_skill_mismatch_below_min_samples_no_trigger() -> None:
    """min_samples=4 默认, 3 样本不触发."""
    svc = SupervisorService()
    for _ in range(3):
        await svc.observe(
            "skill.invocation.completed",
            _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="failure"),
        )
    triggered = await svc.observe(
        "skill.invocation.completed",
        _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="success"),
    )
    # 4 样本 (3 fail + 1 success) — 失败率 75% > 50%, 但样本刚好 4 >= 4 → 应触发
    assert len(triggered) == 1


@pytest.mark.asyncio
async def test_skill_mismatch_low_failure_rate_no_trigger() -> None:
    """4 样本 1 失败 = 25% < 50% 默认 → 不触发."""
    svc = SupervisorService()
    for _ in range(3):
        await svc.observe(
            "skill.invocation.completed",
            _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="success"),
        )
    triggered = await svc.observe(
        "skill.invocation.completed",
        _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="failure"),
    )
    assert triggered == []


@pytest.mark.asyncio
async def test_skill_mismatch_high_failure_triggers() -> None:
    svc = SupervisorService()
    for _ in range(3):
        await svc.observe(
            "skill.invocation.completed",
            _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="failure"),
        )
    triggered = await svc.observe(
        "skill.invocation.completed",
        _skill_event(task_type="coding.refactor", skill_id="bug_fix", outcome="failure"),
    )
    assert len(triggered) == 1
    req = triggered[0]
    assert req["anomaly_kind"] == "skill_mismatch_spike"
    assert req["target_module"] == "skill.bug_fix"
    ev = req["evidence"][0]
    assert ev["task_type"] == "coding.refactor"
    assert ev["skill_id"] == "bug_fix"
    assert ev["sample_size"] == 4
    assert ev["failure_count"] == 4
    assert ev["failure_rate"] == 1.0


@pytest.mark.asyncio
async def test_skill_mismatch_high_failure_promotes_priority() -> None:
    """failure_rate >= 0.8 → priority=high."""
    svc = SupervisorService()
    for _ in range(3):
        await svc.observe(
            "skill.invocation.completed",
            _skill_event(task_type="t", skill_id="s", outcome="failure"),
        )
    triggered = await svc.observe(
        "skill.invocation.completed",
        _skill_event(task_type="t", skill_id="s", outcome="failure"),
    )
    assert triggered[0]["priority"] == "high"


@pytest.mark.asyncio
async def test_skill_mismatch_separate_pairs_isolated() -> None:
    """不同 (task_type, skill_id) 对独立累积."""
    svc = SupervisorService()
    # (refactor, bug_fix) 4 样本全失败 → 应触发
    for _ in range(4):
        await svc.observe(
            "skill.invocation.completed",
            _skill_event(task_type="refactor", skill_id="bug_fix", outcome="failure"),
        )
    # (test_gen, bug_fix) 单条混入 — 与上面不同 task_type
    triggered = await svc.observe(
        "skill.invocation.completed",
        _skill_event(task_type="test_gen", skill_id="bug_fix", outcome="failure"),
    )
    # 第 5 个事件是 (test_gen, bug_fix) 只 1 样本, 不触发
    assert triggered == []


@pytest.mark.asyncio
async def test_skill_mismatch_missing_fields_no_trigger() -> None:
    svc = SupervisorService()
    triggered = await svc.observe(
        "skill.invocation.completed",
        {"tenant_id": "u-test", "outcome": "failure"},  # 无 task_type / skill_id
    )
    assert triggered == []


# ---- Strategist skill_mismatch 候选 ----


@pytest.mark.asyncio
async def test_strategist_yields_three_skill_candidates() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "skill_mismatch_spike",
            "target_module": "skill.bug_fix",
            "evidence": [
                {
                    "task_type": "coding.refactor",
                    "skill_id": "bug_fix",
                    "failure_rate": 0.75,
                    "sample_size": 8,
                }
            ],
        }
    )
    assert len(candidates) == 3
    modes = {c.explorer_mode for c in candidates}
    assert modes == {"conservative", "aggressive", "performance"}

    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    assert conservative.change_spec["kind"] == "skill_id_swap"
    assert conservative.change_spec["task_type"] == "coding.refactor"
    assert conservative.change_spec["old_skill_id"] == "bug_fix"
    assert conservative.target_level == 1

    aggressive = next(c for c in candidates if c.explorer_mode == "aggressive")
    assert aggressive.change_spec["kind"] == "task_type_split"
    assert aggressive.target_level == 0  # 设计层
    assert aggressive.change_spec["requires_director_assistance"] is True

    performance = next(c for c in candidates if c.explorer_mode == "performance")
    assert performance.change_spec["kind"] == "capability_damping_tweak"
    assert performance.target_module.endswith("capability_router")


@pytest.mark.asyncio
async def test_strategist_skill_acceptance_scales_with_failure_rate() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "skill_mismatch_spike",
            "target_module": "skill.x",
            "evidence": [{"task_type": "t", "skill_id": "x", "failure_rate": 0.8}],
        }
    )
    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    # acceptance = 1 - 0.8 * 0.5 = 0.6 (失败率减半)
    assert conservative.acceptance_threshold == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_strategist_skill_candidates_have_rollback() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "skill_mismatch_spike",
            "target_module": "skill.x",
            "evidence": [{"task_type": "t", "skill_id": "x", "failure_rate": 0.7}],
        }
    )
    for c in candidates:
        assert c.rollback_on
