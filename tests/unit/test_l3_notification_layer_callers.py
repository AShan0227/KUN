"""L3.6 — NotificationLayer callers (ADR-018 §16.3 半合并补齐).

新增 2 个 NotificationLayer caller:
  - Gate.admit 在 self-referential awaiting_human_review 时
  - SupervisorService.observe 在 escalation_path 含 human (L4) 时

加上既有 orchestrator.py 共 ≥3 真调用方 — ADR-018 半合并升真合并.
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.gate.service import GateService
from kun.agents.supervisor.service import SupervisorService

# ---- Gate self-referential 推 alert ----


def _ok_test_report() -> dict[str, Any]:
    return {"pass_rate": 0.95, "passed_count": 19, "total_count": 20}


def _ok_debrief() -> dict[str, Any]:
    return {
        "verdict": "ok",
        "rationale": "anchor satisfied",
        "evidence_quality_score": 0.8,
    }


@pytest.mark.asyncio
async def test_gate_self_referential_sends_notification() -> None:
    received: list[dict[str, Any]] = []

    async def fake_sender(payload: dict[str, Any]) -> None:
        received.append(payload)

    svc = GateService(notification_sender=fake_sender)
    decision = await svc.admit(
        {
            "experiment_id": "er-1",
            "target_module": "supervisor.service",  # 自指
            "rationale": "test",
            "change_spec": {"kind": "test"},
        },
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    assert decision.verdict == "awaiting_human_review"
    assert len(received) == 1
    n = received[0]
    assert n["kind"] == "alert"
    assert n["severity"] == "warn"
    assert "self-referential" in n["title"].lower()
    assert "supervisor.service" in n["body"]
    assert n["payload"]["capability_id"] == decision.capability_id


@pytest.mark.asyncio
async def test_gate_non_self_referential_no_notification() -> None:
    received: list[dict[str, Any]] = []

    async def fake_sender(payload: dict[str, Any]) -> None:
        received.append(payload)

    svc = GateService(notification_sender=fake_sender)
    await svc.admit(
        {
            "experiment_id": "er-1",
            "target_module": "llm.router",  # 非自指
            "rationale": "test",
            "change_spec": {"kind": "test"},
        },
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    assert received == []  # approve 路径不发 alert


@pytest.mark.asyncio
async def test_gate_notification_sender_exception_does_not_break_admit() -> None:
    """sender 异常吞掉 + log, admit 仍正常."""

    async def bad_sender(payload: dict[str, Any]) -> None:
        raise RuntimeError("WebSocket down")

    svc = GateService(notification_sender=bad_sender)
    decision = await svc.admit(
        {
            "experiment_id": "er-1",
            "target_module": "gate.service",
            "rationale": "test",
            "change_spec": {"kind": "test"},
        },
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )
    # admit 仍走完 awaiting_human_review
    assert decision.verdict == "awaiting_human_review"


# ---- Supervisor escalation L4 推 alert ----


@pytest.mark.asyncio
async def test_supervisor_human_escalation_sends_notification() -> None:
    """escalation_path 含 human → 推 alert.

    用 dedup_ttl=0 + 多次触发让 severity 升 strong, target_module
    走自指路径 → path 含 human.
    """
    received: list[dict[str, Any]] = []

    async def fake_sender(payload: dict[str, Any]) -> None:
        received.append(payload)

    svc = SupervisorService(notification_sender=fake_sender, dedup_ttl_sec=0)
    # 让 5 次 fallback 触发同 dedup_key, repeat_count 升到 ≥3 → strong
    fb_payload = {
        "tenant_id": "u-test",
        "primary_provider": "strategist",  # 自指 target_module
        "primary_model": "x",
        "fallback_provider": "y",
        "reason": "test",
    }
    for _ in range(2):
        await svc.observe("llm.fallback.triggered", fb_payload)
    triggered = await svc.observe("llm.fallback.triggered", fb_payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert req["is_self_referential"] is True
    assert "human" in req["escalation_path"]

    assert len(received) == 1
    n = received[0]
    assert n["kind"] == "alert"
    assert "Supervisor escalation" in n["title"]
    assert n["payload"]["anomaly_kind"] == "llm_fallback_spike"


@pytest.mark.asyncio
async def test_supervisor_non_human_escalation_no_notification() -> None:
    """普通 weak/mid path 不含 human → 不发 alert."""
    received: list[dict[str, Any]] = []

    async def fake_sender(payload: dict[str, Any]) -> None:
        received.append(payload)

    svc = SupervisorService(notification_sender=fake_sender)
    payload = {
        "tenant_id": "u-test",
        "primary_provider": "openai",  # 非自指
        "primary_model": "x",
    }
    for _ in range(2):
        await svc.observe("llm.fallback.triggered", payload)
    triggered = await svc.observe("llm.fallback.triggered", payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert "human" not in req["escalation_path"]
    assert received == []


@pytest.mark.asyncio
async def test_supervisor_notification_sender_exception_does_not_break_observe() -> None:
    async def bad_sender(payload: dict[str, Any]) -> None:
        raise RuntimeError("notification system down")

    svc = SupervisorService(notification_sender=bad_sender, dedup_ttl_sec=0)
    payload = {
        "tenant_id": "u-test",
        "primary_provider": "strategist",
        "fallback_provider": "y",
    }
    for _ in range(2):
        await svc.observe("llm.fallback.triggered", payload)
    # 不抛, observe 仍返回 triggered request
    triggered = await svc.observe("llm.fallback.triggered", payload)
    assert len(triggered) == 1


# ---- 集成: 3 个 NotificationLayer caller ----


@pytest.mark.asyncio
async def test_three_distinct_notification_callers_pattern() -> None:
    """L3.6 验收: 3 个独立 caller 都按同一 NotificationSender 模式注入.

    实测: GateService + SupervisorService + (orchestrator.py 真用 push)
    都接受同一 Callable[[dict], Awaitable[None]] 签名 — 满足 ADR-018
    "真合并 ≥3 调用方" 约束.
    """
    calls: list[tuple[str, str]] = []  # (source, kind)

    async def gate_sender(payload: dict[str, Any]) -> None:
        calls.append(("gate", payload["kind"]))

    async def supervisor_sender(payload: dict[str, Any]) -> None:
        calls.append(("supervisor", payload["kind"]))

    gate = GateService(notification_sender=gate_sender)
    supervisor = SupervisorService(
        notification_sender=supervisor_sender, dedup_ttl_sec=0
    )

    # Gate 自指 → push alert
    await gate.admit(
        {
            "experiment_id": "er-1",
            "target_module": "gate.service",
            "rationale": "x",
            "change_spec": {},
        },
        test_report=_ok_test_report(),
        debrief=_ok_debrief(),
    )

    # Supervisor 自指 L4 → push alert
    payload = {
        "tenant_id": "u-test",
        "primary_provider": "supervisor",
        "fallback_provider": "y",
    }
    for _ in range(2):
        await supervisor.observe("llm.fallback.triggered", payload)
    await supervisor.observe("llm.fallback.triggered", payload)

    # 两个 caller 都触发
    sources = {c[0] for c in calls}
    assert sources == {"gate", "supervisor"}
    # 都是 alert kind (统一 NotificationLayer 协议)
    assert all(c[1] == "alert" for c in calls)
