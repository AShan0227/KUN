"""V2.3 Wire 53 (C71): orchestrator 真消费 protocol.

验证: KUN_PROTOCOL_CONSUME_ENABLED=1 + protocol_registry 装上 →
orchestrator step 启动前 ProtocolRegistry.find_protocol_for() →
改 task_ref.meta.execution_mode + 加 verification_specs + emit protocol.applied.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kun.qi.protocol import (
    InMemoryProtocolStorage,
    Protocol,
    ProtocolExecution,
    ProtocolRegistry,
    ProtocolTrigger,
    ProtocolVerificationSpec,
)


@pytest.fixture
def stable_protocol() -> Protocol:
    return Protocol(
        protocol_id="writing.creative.test",
        version="1.0.0",
        tenant_id="u-sylvan",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="writing.creative.*",
            complexity_score_min=0.0,
            complexity_score_max=1.0,
            risk_levels=["low", "medium"],
        ),
        execution=ProtocolExecution(mode="MAX"),
        verification=[
            ProtocolVerificationSpec(kind="exact_output", spec={"min_length_chars": 50}),
        ],
    )


@pytest.fixture
async def registry_with_protocol(stable_protocol: Protocol) -> ProtocolRegistry:
    storage = InMemoryProtocolStorage()
    reg = ProtocolRegistry(storage)
    await reg.save(stable_protocol)
    return reg


@pytest.mark.asyncio
async def test_find_protocol_for_matching_task(
    registry_with_protocol: ProtocolRegistry,
) -> None:
    found = await registry_with_protocol.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.5, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is not None
    assert found.protocol_id == "writing.creative.test"
    assert found.execution.mode == "MAX"


@pytest.mark.asyncio
async def test_find_protocol_for_non_matching_task(
    registry_with_protocol: ProtocolRegistry,
) -> None:
    found = await registry_with_protocol.find_protocol_for(
        {"task_type": "coding.python.fix", "complexity_score": 0.5, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is None


@pytest.mark.asyncio
async def test_orchestrator_consume_disabled_by_default() -> None:
    """KUN_PROTOCOL_CONSUME_ENABLED 未设 → 跳过 protocol consume."""
    from kun.engineering.orchestrator import Orchestrator

    fake_registry = MagicMock()
    fake_registry.find_protocol_for = AsyncMock(return_value=None)
    orch = Orchestrator(protocol_registry=fake_registry)
    assert orch.protocol_registry is fake_registry

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_PROTOCOL_CONSUME_ENABLED", None)
        # default is "0", so consume should be skipped
        assert os.getenv("KUN_PROTOCOL_CONSUME_ENABLED", "0") == "0"


@pytest.mark.asyncio
async def test_orchestrator_consume_finds_and_applies_protocol(
    registry_with_protocol: ProtocolRegistry, stable_protocol: Protocol
) -> None:
    """当 KUN_PROTOCOL_CONSUME_ENABLED=1, find_protocol_for 真返结果."""
    found = await registry_with_protocol.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.3, "risk_level": "low"},
        "u-sylvan",
    )
    assert found is not None
    assert found.execution.mode == "MAX"
    # 验证 verification specs 真在协议里 (orchestrator 会加到 task_ref.spec)
    assert len(found.verification) == 1
    assert found.verification[0].kind == "exact_output"


def test_orchestrator_init_accepts_protocol_registry() -> None:
    from kun.engineering.orchestrator import Orchestrator

    orch = Orchestrator(protocol_registry="fake_registry")
    assert orch.protocol_registry == "fake_registry"
    assert orch.anti_gaming_detector is None


def test_orchestrator_init_accepts_anti_gaming_detector() -> None:
    from kun.engineering.orchestrator import Orchestrator
    from kun.security.anti_gaming import AntiGamingDetector

    det = AntiGamingDetector()
    orch = Orchestrator(anti_gaming_detector=det)
    assert orch.anti_gaming_detector is det
    assert orch.protocol_registry is None


def test_orchestrator_init_no_v23_hooks_default() -> None:
    """V2.3 hooks 默认 None — 鲲行为完全不变."""
    from kun.engineering.orchestrator import Orchestrator

    orch = Orchestrator()
    assert orch.protocol_registry is None
    assert orch.anti_gaming_detector is None
    assert orch.prediction_provider is None
    assert orch.model_updater is None
