"""Supervisor 三级阈值 + 4 级升级路径 (ADR-020 §监督线 + ADR-021 RCDH).

  Severity (异常严重度):
    weak   — 留痕 (低优, 1 次, 单点)
    mid    — push 主线 (中优 + 复发 OR 高优单次)
    strong — Gate pause (高优复发 OR 自指 / repeat ≥ 3)

  EscalationLevel (升级路径):
    L1 role   — 给 owner agent (Director / Executor / Tester / Gate) 推 nudge event
    L2 task   — pause 该 task (Director 主线收到 → 走 plan_review)
    L3 gate   — Gate 拒绝同 target_module 的新 capability admit, 等诊断
    L4 human  — 写 promotion_queue.requires_human_review=True (人工介入)

  规则 (severity → escalation paths):
    weak   → [role]
    mid    → [role, task]
    strong → [role, task, gate]
    self-referential (任何严重度) → 路径末尾追加 human

工程化决策树 (engineering rules, 无 LLM, 与 ADR-024 自指限制配套).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["weak", "mid", "strong"]
EscalationLevel = Literal["role", "task", "gate", "human"]


@dataclass(frozen=True)
class EscalationDecision:
    """单次升级决策 — Supervisor 输出, 给上层调度路由."""

    severity: Severity
    escalation_path: list[EscalationLevel]
    rationale: str
    is_self_referential: bool = False
    target_module: str = ""
    contributing_signals: list[str] = field(default_factory=list)


def _is_self_referential(target_module: str) -> bool:
    """target_module 命中 5 个监督角色之一 → self-referential.

    Wrapper around kun.governance.self_referential.is_self_referential —
    L3.5 集中, 此处保留以兼容 import.
    """
    from kun.governance.self_referential import is_self_referential

    return is_self_referential(target_module)


def compute_severity(
    *,
    priority: str,
    repeat_count: int = 0,
    failure_rate: float | None = None,
    sample_size: int = 0,
) -> tuple[Severity, list[str]]:
    """工程化规则: 异常 priority + repeat + failure_rate + sample_size → Severity.

    规则 (从严到宽):
      strong: priority=high AND (repeat >= 2 OR failure_rate >= 0.8) OR repeat >= 3
      mid:    priority=medium AND (repeat >= 2 OR failure_rate >= 0.5)
              OR priority=high (单次)
      weak:   其他 (priority=low 或 medium 单次)

    Returns: (severity, contributing_signals — 命中规则的 evidence 列表)
    """
    signals: list[str] = []
    p = (priority or "").lower()

    is_high_rate = failure_rate is not None and failure_rate >= 0.8
    is_mid_rate = failure_rate is not None and failure_rate >= 0.5

    if p == "high" and (repeat_count >= 2 or is_high_rate):
        signals.append(f"priority=high+repeat={repeat_count}+rate={failure_rate}")
        return "strong", signals
    if repeat_count >= 3:
        signals.append(f"repeat_count={repeat_count}>=3")
        return "strong", signals

    if p == "medium" and (repeat_count >= 2 or is_mid_rate):
        signals.append(f"priority=medium+repeat={repeat_count}+rate={failure_rate}")
        return "mid", signals
    if p == "high":
        signals.append("priority=high+single_occurrence")
        return "mid", signals

    signals.append(f"priority={p}+repeat={repeat_count}")
    return "weak", signals


def escalation_path_for(
    severity: Severity, *, is_self_referential: bool = False
) -> list[EscalationLevel]:
    """Severity → 升级路径 (L1→L2→L3→L4 累加).

    自指任意严重度 → 路径末尾追加 human.
    """
    if severity == "weak":
        base: list[EscalationLevel] = ["role"]
    elif severity == "mid":
        base = ["role", "task"]
    else:  # strong
        base = ["role", "task", "gate"]

    if is_self_referential and "human" not in base:
        base.append("human")
    return base


def decide_escalation(
    *,
    priority: str,
    target_module: str,
    repeat_count: int = 0,
    failure_rate: float | None = None,
    sample_size: int = 0,
) -> EscalationDecision:
    """主入口: anomaly 信号 → EscalationDecision.

    流程:
      1. compute_severity (engineering rules)
      2. _is_self_referential 检查
      3. escalation_path_for severity + self_ref
      4. 组合 rationale
    """
    severity, severity_signals = compute_severity(
        priority=priority,
        repeat_count=repeat_count,
        failure_rate=failure_rate,
        sample_size=sample_size,
    )
    self_ref = _is_self_referential(target_module)
    path = escalation_path_for(severity, is_self_referential=self_ref)

    rationale_parts = [f"severity={severity}"]
    rationale_parts.extend(severity_signals)
    if self_ref:
        rationale_parts.append(f"self_referential={target_module}")
    rationale_parts.append(f"path={'/'.join(path)}")

    return EscalationDecision(
        severity=severity,
        escalation_path=path,
        rationale=" | ".join(rationale_parts),
        is_self_referential=self_ref,
        target_module=target_module,
        contributing_signals=severity_signals,
    )


__all__ = [
    "EscalationDecision",
    "EscalationLevel",
    "Severity",
    "compute_severity",
    "decide_escalation",
    "escalation_path_for",
]
