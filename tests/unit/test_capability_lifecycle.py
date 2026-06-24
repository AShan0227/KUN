"""V7 Phase D — capability lifecycle gate enforcement 单测.

V7 §15 + §12.2 9 阶段 lifecycle 验证.
"""

from __future__ import annotations

import pytest
from kun.governance.capability_lifecycle import (
    STRICT_ACCEPTANCE_STAGES,
    CapabilityLifecycleError,
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
)


@pytest.mark.unit
class TestStageEnum:
    def test_9_stages(self) -> None:
        """V7 §15 9 阶段总流程 (enum 含 10 entry, Rollback 9a + Retire 9b)."""
        stages = list(CapabilityLifecycleStage)
        # 严格按 V7 §12.2 阶段图 9 阶段: 1-7 主线 + 8 Monitor + 9 Rollback / Retire
        # enum 设计 10 个 entry 是因为 Rollback 跟 Retire 区分 (一个是"主动回退", 一个是"长期无人用退休")
        assert len(stages) == 10  # 9 阶段总流程, Rollback 9a + Retire 9b 单列
        names = [s.value for s in stages]
        assert names[:8] == [
            "observation",
            "candidate",
            "replay",
            "holdout",
            "shadow",
            "canary",
            "production",
            "monitor",
        ]
        assert "rollback" in names
        assert "retire" in names

    def test_strict_acceptance_5_stages(self) -> None:
        """V7 §12.2 严格验收 5 阶段."""
        assert len(STRICT_ACCEPTANCE_STAGES) == 5
        assert CapabilityLifecycleStage.REPLAY in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.HOLDOUT in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.SHADOW in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.CANARY in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.PRODUCTION in STRICT_ACCEPTANCE_STAGES
        # OBSERVATION / CANDIDATE / MONITOR / ROLLBACK / RETIRE 不在
        assert CapabilityLifecycleStage.OBSERVATION not in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.CANDIDATE not in STRICT_ACCEPTANCE_STAGES
        assert CapabilityLifecycleStage.MONITOR not in STRICT_ACCEPTANCE_STAGES


@pytest.mark.unit
class TestCanTransition:
    def test_legal_happy_path(self) -> None:
        svc = CapabilityLifecycleService()
        assert svc.can_transition(
            CapabilityLifecycleStage.OBSERVATION, CapabilityLifecycleStage.CANDIDATE
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.CANDIDATE, CapabilityLifecycleStage.REPLAY
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.REPLAY, CapabilityLifecycleStage.HOLDOUT
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.HOLDOUT, CapabilityLifecycleStage.SHADOW
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.SHADOW, CapabilityLifecycleStage.CANARY
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.CANARY, CapabilityLifecycleStage.PRODUCTION
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.PRODUCTION, CapabilityLifecycleStage.MONITOR
        )

    def test_skip_stages_illegal(self) -> None:
        """不允许跳级."""
        svc = CapabilityLifecycleService()
        assert not svc.can_transition(
            CapabilityLifecycleStage.CANDIDATE, CapabilityLifecycleStage.SHADOW
        )
        assert not svc.can_transition(
            CapabilityLifecycleStage.REPLAY, CapabilityLifecycleStage.PRODUCTION
        )
        assert not svc.can_transition(
            CapabilityLifecycleStage.HOLDOUT, CapabilityLifecycleStage.CANARY
        )

    def test_retire_from_anywhere(self) -> None:
        """Retire 是终态, 从大部分阶段都可达 (能力被退休)."""
        svc = CapabilityLifecycleService()
        assert svc.can_transition(
            CapabilityLifecycleStage.OBSERVATION, CapabilityLifecycleStage.RETIRE
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.HOLDOUT, CapabilityLifecycleStage.RETIRE
        )
        assert svc.can_transition(
            CapabilityLifecycleStage.MONITOR, CapabilityLifecycleStage.RETIRE
        )

    def test_rollback_terminal(self) -> None:
        """Rollback 只能到 Retire, 不能回退到任意阶段."""
        svc = CapabilityLifecycleService()
        assert svc.can_transition(
            CapabilityLifecycleStage.ROLLBACK, CapabilityLifecycleStage.RETIRE
        )
        assert not svc.can_transition(
            CapabilityLifecycleStage.ROLLBACK, CapabilityLifecycleStage.CANDIDATE
        )


@pytest.mark.unit
class TestValidateTransition:
    def test_illegal_transition_raises(self) -> None:
        svc = CapabilityLifecycleService()
        with pytest.raises(CapabilityLifecycleError, match="Illegal transition"):
            svc.validate_transition(
                CapabilityLifecycleStage.CANDIDATE,
                CapabilityLifecycleStage.SHADOW,  # skip REPLAY
                evidence_refs=[],
            )

    def test_candidate_to_replay_requires_three_evidence(self) -> None:
        """V7 §12.3 三类证据 (strategy_replay_report / process_audit / capability_candidate)."""
        svc = CapabilityLifecycleService()
        with pytest.raises(CapabilityLifecycleError, match="three evidence kinds"):
            svc.validate_transition(
                CapabilityLifecycleStage.CANDIDATE,
                CapabilityLifecycleStage.REPLAY,
                evidence_refs=["strategy_replay_report:rep-001"],  # only 1
            )

    def test_candidate_to_replay_with_all_three_evidence_passes(self) -> None:
        svc = CapabilityLifecycleService()
        # 不抛
        svc.validate_transition(
            CapabilityLifecycleStage.CANDIDATE,
            CapabilityLifecycleStage.REPLAY,
            evidence_refs=[
                "strategy_replay_report:rep-001",
                "process_audit:aud-001",
                "capability_candidate:cap-001",
            ],
        )

    def test_canary_to_production_requires_user_approval(self) -> None:
        """V7 §12.2 production flip 必须 user approval."""
        svc = CapabilityLifecycleService()
        with pytest.raises(CapabilityLifecycleError, match="user approval ticket"):
            svc.validate_transition(
                CapabilityLifecycleStage.CANARY,
                CapabilityLifecycleStage.PRODUCTION,
                evidence_refs=[],
                user_approval_ticket_id=None,
            )

    def test_canary_to_production_with_user_approval_passes(self) -> None:
        svc = CapabilityLifecycleService()
        # 不抛
        svc.validate_transition(
            CapabilityLifecycleStage.CANARY,
            CapabilityLifecycleStage.PRODUCTION,
            evidence_refs=[],
            user_approval_ticket_id="ticket-001",
        )

    def test_can_disable_user_approval_for_testing(self) -> None:
        """允许 test 时关掉 user approval enforce."""
        svc = CapabilityLifecycleService(require_user_approval_for_production=False)
        svc.validate_transition(
            CapabilityLifecycleStage.CANARY,
            CapabilityLifecycleStage.PRODUCTION,
            evidence_refs=[],
            user_approval_ticket_id=None,
        )

    def test_can_disable_three_evidence_for_testing(self) -> None:
        svc = CapabilityLifecycleService(require_three_evidence_for_replay=False)
        svc.validate_transition(
            CapabilityLifecycleStage.CANDIDATE,
            CapabilityLifecycleStage.REPLAY,
            evidence_refs=[],
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_emits_record() -> None:
    """transition() 调用 emitter, 跟其他 V7 service 一致 (V7 §13.6)."""
    captured: list = []

    async def _emitter(record):
        captured.append(record)

    svc = CapabilityLifecycleService(transition_emitter=_emitter)
    record = await svc.transition(
        capability_id="cap-001",
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
        decision_rationale="启 strategy replay 输出 capability_candidate",
    )
    assert record.capability_id == "cap-001"
    assert record.from_stage == CapabilityLifecycleStage.OBSERVATION
    assert record.to_stage == CapabilityLifecycleStage.CANDIDATE
    assert len(captured) == 1
    assert captured[0].transition_id == record.transition_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_emitter_failure_does_not_crash_service() -> None:
    """V7 §13.6 emitter 异常吞掉, 不打挂 service 主路径."""

    async def _bad_emitter(record):
        raise RuntimeError("DB down")

    svc = CapabilityLifecycleService(transition_emitter=_bad_emitter)
    record = await svc.transition(
        capability_id="cap-001",
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
    )
    assert record.transition_id  # 仍然产 record


@pytest.mark.unit
class TestRuntimeEnabledRule:
    """V7 §15.2 runtime_enabled=true 只对 Production / Monitor 阶段有效."""

    def test_production_runtime_enabled(self) -> None:
        assert CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.PRODUCTION
        )
        assert CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.MONITOR
        )

    def test_non_production_runtime_disabled(self) -> None:
        assert not CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.REPLAY
        )
        assert not CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.SHADOW
        )
        assert not CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.CANARY
        )
        assert not CapabilityLifecycleService.is_runtime_enabled_stage(
            CapabilityLifecycleStage.CANDIDATE
        )

    def test_strict_acceptance_stages_check(self) -> None:
        assert CapabilityLifecycleService.is_strict_acceptance_stage(
            CapabilityLifecycleStage.REPLAY
        )
        assert CapabilityLifecycleService.is_strict_acceptance_stage(
            CapabilityLifecycleStage.PRODUCTION
        )
        assert not CapabilityLifecycleService.is_strict_acceptance_stage(
            CapabilityLifecycleStage.OBSERVATION
        )
        assert not CapabilityLifecycleService.is_strict_acceptance_stage(
            CapabilityLifecycleStage.MONITOR
        )
