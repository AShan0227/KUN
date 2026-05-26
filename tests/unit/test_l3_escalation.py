"""L3.4 — 监督线三级阈值 + 4 级升级路径 单测."""

from __future__ import annotations

import pytest
from kun.agents.supervisor.escalation import (
    EscalationDecision,
    _is_self_referential,
    compute_severity,
    decide_escalation,
    escalation_path_for,
)
from kun.agents.supervisor.service import SupervisorService

# ---- compute_severity ----


def test_severity_weak_low_priority_single() -> None:
    sev, signals = compute_severity(priority="low", repeat_count=1)
    assert sev == "weak"
    assert signals


def test_severity_weak_medium_single() -> None:
    sev, _ = compute_severity(priority="medium", repeat_count=1)
    assert sev == "weak"


def test_severity_mid_medium_with_repeat() -> None:
    sev, _ = compute_severity(priority="medium", repeat_count=2)
    assert sev == "mid"


def test_severity_mid_medium_with_failure_rate() -> None:
    sev, _ = compute_severity(priority="medium", repeat_count=1, failure_rate=0.6)
    assert sev == "mid"


def test_severity_mid_high_single_occurrence() -> None:
    sev, _ = compute_severity(priority="high", repeat_count=1)
    assert sev == "mid"


def test_severity_strong_high_with_repeat() -> None:
    sev, _ = compute_severity(priority="high", repeat_count=2)
    assert sev == "strong"


def test_severity_strong_high_with_failure_rate() -> None:
    sev, _ = compute_severity(priority="high", repeat_count=1, failure_rate=0.85)
    assert sev == "strong"


def test_severity_strong_any_priority_with_3_repeats() -> None:
    """repeat_count >= 3 强制 strong 不论 priority."""
    sev, _ = compute_severity(priority="low", repeat_count=3)
    assert sev == "strong"


# ---- escalation_path_for ----


def test_path_weak_is_role_only() -> None:
    assert escalation_path_for("weak") == ["role"]


def test_path_mid_is_role_task() -> None:
    assert escalation_path_for("mid") == ["role", "task"]


def test_path_strong_is_role_task_gate() -> None:
    assert escalation_path_for("strong") == ["role", "task", "gate"]


def test_path_self_referential_appends_human() -> None:
    assert escalation_path_for("weak", is_self_referential=True) == ["role", "human"]
    assert escalation_path_for("strong", is_self_referential=True) == [
        "role",
        "task",
        "gate",
        "human",
    ]


# ---- _is_self_referential ----


def test_self_referential_role_prefixes() -> None:
    assert _is_self_referential("supervisor")
    assert _is_self_referential("supervisor.service")
    assert _is_self_referential("kun.agents.gate")
    assert _is_self_referential("kun/agents/strategist/service")
    assert _is_self_referential("external_supervisor.modes")


def test_self_referential_rejects_other_modules() -> None:
    assert _is_self_referential("executor.tool_calling") is False
    assert _is_self_referential("llm.router") is False
    assert _is_self_referential("skill.bug_fix") is False


# ---- decide_escalation ----


def test_decide_escalation_weak_path() -> None:
    d = decide_escalation(priority="low", target_module="llm.router")
    assert isinstance(d, EscalationDecision)
    assert d.severity == "weak"
    assert d.escalation_path == ["role"]
    assert d.is_self_referential is False
    assert d.target_module == "llm.router"


def test_decide_escalation_strong_path_with_self_referential() -> None:
    d = decide_escalation(
        priority="high", target_module="supervisor.service", repeat_count=3
    )
    assert d.severity == "strong"
    assert d.escalation_path == ["role", "task", "gate", "human"]
    assert d.is_self_referential is True


def test_decide_escalation_mid_with_failure_rate() -> None:
    d = decide_escalation(
        priority="medium",
        target_module="executor.x",
        failure_rate=0.7,
        repeat_count=1,
    )
    assert d.severity == "mid"
    assert d.escalation_path == ["role", "task"]


def test_decide_escalation_rationale_contains_severity() -> None:
    d = decide_escalation(priority="high", target_module="x", repeat_count=2)
    assert "severity=strong" in d.rationale
    assert "path=" in d.rationale


# ---- SupervisorService integration ----


@pytest.mark.asyncio
async def test_supervisor_request_carries_escalation_fields() -> None:
    """SupervisorService _build_request 现在带 severity / path / repeat_count."""
    svc = SupervisorService()
    payload = {
        "tenant_id": "u-test",
        "input_tokens": 100_000,
        "provider": "anthropic",
    }
    for _ in range(2):
        await svc.observe("llm.invoke.completed", payload)
    triggered = await svc.observe("llm.invoke.completed", payload)
    assert len(triggered) == 1
    req = triggered[0]
    # L3.4 字段
    assert "severity" in req
    assert "escalation_path" in req
    assert "is_self_referential" in req
    assert "repeat_count" in req
    assert req["repeat_count"] == 1  # 首次进入 (dedup 不重复)
    assert req["severity"] in {"weak", "mid", "strong"}
    assert isinstance(req["escalation_path"], list)


@pytest.mark.asyncio
async def test_supervisor_repeat_count_increments_across_triggers() -> None:
    """同 dedup_key 每次成功触发 → repeat_count 递增 (跨 dedup_ttl 累计)."""
    svc = SupervisorService(dedup_ttl_sec=0)  # 立即去 dedup
    payload = {
        "tenant_id": "u-test",
        "task_type": "coding.refactor",
        "skill_id": "bug_fix",
        "outcome": "failure",
    }
    # 头 3 事件累积, 不触发 (min_samples=4)
    for _ in range(3):
        triggered = await svc.observe("skill.invocation.completed", payload)
        assert triggered == []
    # 第 4 事件: 触发 repeat=1
    t = await svc.observe("skill.invocation.completed", payload)
    assert t[0]["repeat_count"] == 1
    # 第 5 事件: 又触发 (dedup=0, 立即放行) → repeat=2
    t = await svc.observe("skill.invocation.completed", payload)
    assert t[0]["repeat_count"] == 2
    # 第 6 事件: repeat=3 — 强制升级 severity=strong (rule: repeat_count >= 3 → strong)
    t = await svc.observe("skill.invocation.completed", payload)
    assert t[0]["repeat_count"] == 3
    assert t[0]["severity"] == "strong"


@pytest.mark.asyncio
async def test_supervisor_severity_strong_path_includes_gate() -> None:
    """高优 + repeat → strong → path 含 gate."""
    svc = SupervisorService(dedup_ttl_sec=0)
    payload = {"tenant_id": "u-test", "task_type": "t", "skill_id": "s", "outcome": "failure"}
    for _ in range(4):
        await svc.observe("skill.invocation.completed", payload)
    # 第 5 个触发, repeat=2; failure_rate=1.0+priority=high → strong
    t = await svc.observe("skill.invocation.completed", payload)
    req = t[0]
    assert req["severity"] == "strong"
    assert "gate" in req["escalation_path"]


@pytest.mark.asyncio
async def test_supervisor_severity_uses_failure_rate_from_evidence() -> None:
    """skill_mismatch evidence 含 failure_rate → 高失败率 → strong severity."""
    svc = SupervisorService()
    payload = {
        "tenant_id": "u-test",
        "task_type": "t",
        "skill_id": "s",
        "outcome": "failure",
    }
    # 4 失败 = failure_rate=1.0, priority=high (按 _check_skill_mismatch 规则 >=0.8)
    for _ in range(3):
        await svc.observe("skill.invocation.completed", payload)
    triggered = await svc.observe("skill.invocation.completed", payload)
    req = triggered[0]
    # priority=high + repeat=1 默认 → mid; high+failure_rate=1.0 → strong
    assert req["priority"] == "high"
    assert req["severity"] == "strong"
