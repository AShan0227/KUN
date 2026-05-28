"""V7 §15 + §12.2 capability lifecycle — 9 阶段 lifecycle + gate enforcement.

V7 严格约束: 任何 RSI candidate / 启 capability_candidate 必须走完整 9 阶段
才能进 production runtime, 其中 Replay → Holdout → Shadow → Canary → Production
是**严格验收 5 阶段**。

```text
前置 (Observation / Candidate)
   ↓
验收 5 阶段 (Replay / Holdout / Shadow / Canary / Production)
   ↓
后置 (Monitor / Rollback / Retire)
```

V7 §12.0 + §15 + §16.8 全部强制:
- KUN Runtime 默认只能消费 production 阶段能力 (`runtime_enabled=true` 仅对
  Production 有效)
- production flip 必须 explicit user approval (CollaborationTicket)
- 失败可 rollback → Rollback / Retire 阶段
- 任何阶段缺三类证据 (strategy_replay_report / process_audit /
  capability_candidate) 不能进 Replay 阶段

本 module 提供:
1. ``CapabilityLifecycleStage`` enum 9 阶段
2. ``LifecycleTransition`` frozen dataclass — 状态转移记录
3. ``CapabilityLifecycleService`` — gate enforcement / 转移 / rollback
4. 每阶段 entry/exit 条件 (软 enforcement, 调用方传)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.capability_lifecycle")


class CapabilityLifecycleStage(StrEnum):
    """V7 §15 能力 lifecycle 9 阶段."""

    # 前置 (前 2 阶段)
    OBSERVATION = "observation"  # 1. 发现机会
    CANDIDATE = "candidate"  # 2. 启写 capability_candidate (含三类证据)

    # 严格验收 5 阶段 (V7 §12.2)
    REPLAY = "replay"  # 3. 用历史数据重放, 验证不劣于 baseline
    HOLDOUT = "holdout"  # 4. 隔离 held-out 任务测候选
    SHADOW = "shadow"  # 5. 与 production 并行跑, 对比但不切换
    CANARY = "canary"  # 6. 小比例切到候选 (5-15%), 监控
    PRODUCTION = "production"  # 7. 全量切换, runtime_enabled=true (必须 user approval)

    # 后置 (后 2 阶段)
    MONITOR = "monitor"  # 8. 持续观察指标, 回归则 rollback
    ROLLBACK = "rollback"  # 9a. 回退到 baseline
    RETIRE = "retire"  # 9b. 长期无人使用 → 退休


# 严格验收 5 阶段的集合 (V7 §12.2)
STRICT_ACCEPTANCE_STAGES: frozenset[CapabilityLifecycleStage] = frozenset(
    {
        CapabilityLifecycleStage.REPLAY,
        CapabilityLifecycleStage.HOLDOUT,
        CapabilityLifecycleStage.SHADOW,
        CapabilityLifecycleStage.CANARY,
        CapabilityLifecycleStage.PRODUCTION,
    }
)

# 合法的阶段转移 (邻接表). 不允许跳级.
_LEGAL_TRANSITIONS: dict[CapabilityLifecycleStage, frozenset[CapabilityLifecycleStage]] = {
    CapabilityLifecycleStage.OBSERVATION: frozenset(
        {CapabilityLifecycleStage.CANDIDATE, CapabilityLifecycleStage.RETIRE}
    ),
    CapabilityLifecycleStage.CANDIDATE: frozenset(
        {CapabilityLifecycleStage.REPLAY, CapabilityLifecycleStage.RETIRE}
    ),
    CapabilityLifecycleStage.REPLAY: frozenset(
        {
            CapabilityLifecycleStage.HOLDOUT,
            CapabilityLifecycleStage.RETIRE,
            CapabilityLifecycleStage.ROLLBACK,
        }
    ),
    CapabilityLifecycleStage.HOLDOUT: frozenset(
        {
            CapabilityLifecycleStage.SHADOW,
            CapabilityLifecycleStage.RETIRE,
            CapabilityLifecycleStage.ROLLBACK,
        }
    ),
    CapabilityLifecycleStage.SHADOW: frozenset(
        {
            CapabilityLifecycleStage.CANARY,
            CapabilityLifecycleStage.RETIRE,
            CapabilityLifecycleStage.ROLLBACK,
        }
    ),
    CapabilityLifecycleStage.CANARY: frozenset(
        {
            CapabilityLifecycleStage.PRODUCTION,
            CapabilityLifecycleStage.RETIRE,
            CapabilityLifecycleStage.ROLLBACK,
        }
    ),
    CapabilityLifecycleStage.PRODUCTION: frozenset(
        {CapabilityLifecycleStage.MONITOR, CapabilityLifecycleStage.ROLLBACK}
    ),
    CapabilityLifecycleStage.MONITOR: frozenset(
        {
            CapabilityLifecycleStage.PRODUCTION,  # stay in production after monitor confirms healthy
            CapabilityLifecycleStage.ROLLBACK,
            CapabilityLifecycleStage.RETIRE,
        }
    ),
    CapabilityLifecycleStage.ROLLBACK: frozenset({CapabilityLifecycleStage.RETIRE}),
    CapabilityLifecycleStage.RETIRE: frozenset(),  # 终态
}


@dataclass(frozen=True)
class LifecycleTransition:
    """状态转移记录 — frozen, 跟其他 V7 agent IO 契约一致 (V7 §13.6)."""

    transition_id: str
    capability_id: str
    from_stage: CapabilityLifecycleStage
    to_stage: CapabilityLifecycleStage
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decision_rationale: str = ""
    user_approval_ticket_id: str | None = None  # production flip 必须有 user approval
    evidence_refs: list[str] = field(default_factory=list)  # strategy_replay_report 等
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


class CapabilityLifecycleError(ValueError):
    """Raised when lifecycle transition violates V7 §12.2 / §15 rules."""


class CapabilityLifecycleService:
    """启 (Qi) 能力 lifecycle 治理服务 (V7 §9.6 / §12 / §15).

    职责:
    1. 验证 stage 转移合法 (不允许跳级 — Replay 必须经 Holdout 才到 Shadow)
    2. 强 enforce: production flip 必须 user approval
    3. 强 enforce: capability_candidate 进 Replay 必须有三类证据
    4. emit transition 给 emitter (DB 落地由 caller 实装)

    使用 emitter callback 模式 (V7 §13.6 frozen_dataclass_agent_io_contract):
    service 不直接接 DB, 测试用 fake, prod 接真 writer.
    """

    def __init__(
        self,
        *,
        transition_emitter: Callable[[LifecycleTransition], Any] | None = None,
        require_user_approval_for_production: bool = True,
        require_three_evidence_for_replay: bool = True,
    ) -> None:
        self._emitter = transition_emitter
        self._require_user_approval = require_user_approval_for_production
        self._require_three_evidence = require_three_evidence_for_replay

    def can_transition(
        self,
        from_stage: CapabilityLifecycleStage,
        to_stage: CapabilityLifecycleStage,
    ) -> bool:
        """Check if a stage transition is legal (邻接表)."""
        return to_stage in _LEGAL_TRANSITIONS.get(from_stage, frozenset())

    def validate_transition(
        self,
        from_stage: CapabilityLifecycleStage,
        to_stage: CapabilityLifecycleStage,
        *,
        evidence_refs: list[str],
        user_approval_ticket_id: str | None = None,
    ) -> None:
        """Validate a transition. Raises CapabilityLifecycleError on violation.

        Rules (V7 §12.2 / §15):
        1. transition must be in _LEGAL_TRANSITIONS (no skipping)
        2. CANDIDATE → REPLAY: require 3 evidence types (strategy_replay_report,
           process_audit, capability_candidate)
        3. CANARY → PRODUCTION: require user_approval_ticket_id
        """
        if not self.can_transition(from_stage, to_stage):
            raise CapabilityLifecycleError(
                f"Illegal transition: {from_stage.value} → {to_stage.value}. "
                f"V7 §15 capability lifecycle 不允许跳级. "
                f"Legal next stages from {from_stage.value}: "
                f"{[s.value for s in _LEGAL_TRANSITIONS.get(from_stage, frozenset())]}"
            )

        # Rule 2: CANDIDATE → REPLAY needs three-evidence (V7 §12.3)
        if (
            from_stage == CapabilityLifecycleStage.CANDIDATE
            and to_stage == CapabilityLifecycleStage.REPLAY
            and self._require_three_evidence
        ):
            required_evidence_kinds = {
                "strategy_replay_report",
                "process_audit",
                "capability_candidate",
            }
            evidence_kinds = {
                ref.split(":", 1)[0] if ":" in ref else ref
                for ref in evidence_refs
            }
            missing = required_evidence_kinds - evidence_kinds
            if missing:
                raise CapabilityLifecycleError(
                    f"CANDIDATE → REPLAY requires three evidence kinds "
                    f"(V7 §12.3). Missing: {sorted(missing)}. "
                    f"Got evidence refs: {evidence_refs}"
                )

        # Rule 3: CANARY → PRODUCTION needs user approval (V7 §12.2)
        if (
            from_stage == CapabilityLifecycleStage.CANARY
            and to_stage == CapabilityLifecycleStage.PRODUCTION
            and self._require_user_approval
            and not user_approval_ticket_id
        ):
            raise CapabilityLifecycleError(
                "CANARY → PRODUCTION requires explicit user approval ticket "
                "(V7 §12.2). Pass user_approval_ticket_id from a closed "
                "CollaborationTicket."
            )

    async def transition(
        self,
        *,
        capability_id: str,
        from_stage: CapabilityLifecycleStage,
        to_stage: CapabilityLifecycleStage,
        evidence_refs: list[str] | None = None,
        user_approval_ticket_id: str | None = None,
        decision_rationale: str = "",
        metrics_snapshot: dict[str, Any] | None = None,
    ) -> LifecycleTransition:
        """Validate + emit a stage transition.

        Returns the LifecycleTransition record. Caller can use ID to
        cross-reference in capability_card / audit log.
        """
        ev_refs = list(evidence_refs or [])
        self.validate_transition(
            from_stage,
            to_stage,
            evidence_refs=ev_refs,
            user_approval_ticket_id=user_approval_ticket_id,
        )

        from kun.core.ids import new_id

        record = LifecycleTransition(
            transition_id=new_id("lifecycle_transition"),
            capability_id=capability_id,
            from_stage=from_stage,
            to_stage=to_stage,
            decision_rationale=decision_rationale,
            user_approval_ticket_id=user_approval_ticket_id,
            evidence_refs=ev_refs,
            metrics_snapshot=dict(metrics_snapshot or {}),
        )

        log.info(
            "lifecycle.transition",
            capability_id=capability_id,
            from_stage=from_stage.value,
            to_stage=to_stage.value,
            transition_id=record.transition_id,
            has_user_approval=bool(user_approval_ticket_id),
            n_evidence=len(ev_refs),
        )

        if self._emitter is not None:
            try:
                maybe_aw = self._emitter(record)
                if hasattr(maybe_aw, "__await__"):
                    await maybe_aw  # type: ignore[misc]
            except Exception as e:
                log.warning(
                    "lifecycle.emit_failed",
                    capability_id=capability_id,
                    transition_id=record.transition_id,
                    error=str(e),
                )

        return record

    @staticmethod
    def is_strict_acceptance_stage(stage: CapabilityLifecycleStage) -> bool:
        """V7 §12.2 严格验收 5 阶段."""
        return stage in STRICT_ACCEPTANCE_STAGES

    @staticmethod
    def is_runtime_enabled_stage(stage: CapabilityLifecycleStage) -> bool:
        """V7 §15.2 只有 Production / Monitor 阶段允许 runtime_enabled=true."""
        return stage in {
            CapabilityLifecycleStage.PRODUCTION,
            CapabilityLifecycleStage.MONITOR,
        }


__all__ = [
    "STRICT_ACCEPTANCE_STAGES",
    "CapabilityLifecycleError",
    "CapabilityLifecycleService",
    "CapabilityLifecycleStage",
    "LifecycleTransition",
]
