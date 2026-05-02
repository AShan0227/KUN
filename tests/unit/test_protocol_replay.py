from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kun.qi.auto_promote import auto_promote_protocols
from kun.qi.protocol import (
    InMemoryProtocolStorage,
    Protocol,
    ProtocolExecution,
    ProtocolHermesTemplate,
    ProtocolRegistry,
    ProtocolSkillStep,
    ProtocolTrigger,
    ProtocolVerificationSpec,
)
from kun.qi.protocol_replay import PROMOTION_EVIDENCE_SOURCE, ProtocolReplayEvaluator


def _complete_protocol(*, status: str = "experimental") -> Protocol:
    return Protocol(
        protocol_id="ops.product.iteration",
        version="0.1.0",
        tenant_id="u-test",
        status=status,  # type: ignore[arg-type]
        trigger=ProtocolTrigger(task_type_pattern="ops.product.*"),
        execution=ProtocolExecution(mode="SMART", max_steps=6, expected_cost_usd=0.08),
        skill_chain=[
            ProtocolSkillStep(skill="market_research.scan"),
            ProtocolSkillStep(skill="task_plan.write"),
        ],
        hermes_template=ProtocolHermesTemplate(
            system_prompt_addon="Use the V4 task brief structure and expose risks plainly.",
        ),
        verification=[
            ProtocolVerificationSpec(kind="rubric", spec={"min_score": 0.7}),
        ],
    )


@pytest.mark.asyncio
async def test_protocol_replay_writes_smoke_evidence_for_complete_protocol() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await registry.save(_complete_protocol())

    result = await ProtocolReplayEvaluator().evaluate_missing_evidence(registry, "u-test")

    assert result["updated"] == 1
    proto = await registry.get("u-test", "ops.product.iteration", "0.1.0")
    assert proto is not None
    evidence = proto.metadata["promotion_evidence"]
    assert evidence["runs"] == 5
    assert evidence["guardrail_pass"] is True
    assert evidence["source"] == PROMOTION_EVIDENCE_SOURCE
    assert evidence["win_rate"] >= 0.65
    assert proto.metadata["protocol_replay"]["source"] == PROMOTION_EVIDENCE_SOURCE


@pytest.mark.asyncio
async def test_protocol_replay_blocks_incomplete_protocol_without_fake_evidence() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await registry.save(
        Protocol(
            protocol_id="too.broad",
            version="0.1.0",
            tenant_id="u-test",
            status="experimental",
            trigger=ProtocolTrigger(task_type_pattern="*"),
            execution=ProtocolExecution(),
        )
    )

    result = await ProtocolReplayEvaluator().evaluate_missing_evidence(registry, "u-test")

    assert result["blocked"] == 1
    proto = await registry.get("u-test", "too.broad", "0.1.0")
    assert proto is not None
    assert "promotion_evidence" not in proto.metadata
    assert "trigger_too_broad" in proto.metadata["protocol_replay"]["blocked_reasons"]


@pytest.mark.asyncio
async def test_auto_promote_uses_replay_evidence_to_move_experimental_to_shadow() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await registry.save(_complete_protocol())
    app = SimpleNamespace(state=SimpleNamespace(protocol_registry=registry))

    with patch.dict(
        "os.environ",
        {
            "KUN_PROTOCOL_AUTO_PROMOTE_ENABLED": "1",
            "KUN_PROTOCOL_REPLAY_EVALUATOR_ENABLED": "1",
        },
    ):
        result = await auto_promote_protocols(app, "u-test")

    assert result["promoted"] == 1
    assert result["replay_evidence"]["updated"] == 1
    proto = await registry.get("u-test", "ops.product.iteration", "0.1.0")
    assert proto is not None
    assert proto.status == "shadow"


@pytest.mark.asyncio
async def test_replay_evidence_does_not_jump_shadow_to_canary() -> None:
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    await registry.save(_complete_protocol(status="shadow"))
    app = SimpleNamespace(state=SimpleNamespace(protocol_registry=registry))

    with patch.dict(
        "os.environ",
        {
            "KUN_PROTOCOL_AUTO_PROMOTE_ENABLED": "1",
            "KUN_PROTOCOL_REPLAY_EVALUATOR_ENABLED": "1",
        },
    ):
        result = await auto_promote_protocols(app, "u-test")

    assert result["promoted"] == 0
    assert result["kept"] == 1
    proto = await registry.get("u-test", "ops.product.iteration", "0.1.0")
    assert proto is not None
    assert proto.status == "shadow"
    assert proto.metadata["promotion_evidence"]["runs"] == 5
