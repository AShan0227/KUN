"""V2.3+ 周边: auto_promote / anti_gaming_learner / verification_auto_gen / multi_window."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kun.qi.anti_gaming_learner import (
    AntiGamingLearner,
    get_anti_gaming_learner,
    reset_anti_gaming_learner,
)
from kun.qi.auto_promote import auto_promote_protocols
from kun.qi.multi_window import get_active_windows, is_any_window_active
from kun.qi.protocol import (
    InMemoryProtocolStorage,
    Protocol,
    ProtocolExecution,
    ProtocolRegistry,
    ProtocolTrigger,
)
from kun.qi.verification_auto_gen import (
    TaskOutcomeSample,
    VerificationAutoGen,
    get_verification_auto_gen,
    reset_verification_auto_gen,
)

# ===== Auto Promote =====


@pytest.mark.asyncio
async def test_auto_promote_low_score_kept() -> None:
    """darwin_best_score < 0.5 → 不 promote."""
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    proto = Protocol(
        protocol_id="x.test",
        version="1.0.0",
        tenant_id="u-test",
        status="experimental",
        trigger=ProtocolTrigger(task_type_pattern="x.*"),
        execution=ProtocolExecution(),
        metadata={"darwin_best_score": 0.3, "runs": 5},
    )
    await registry.save(proto)
    app = SimpleNamespace(state=SimpleNamespace(protocol_registry=registry))
    result = await auto_promote_protocols(app, "u-test")
    assert result["promoted"] == 0
    assert result["kept"] == 1


@pytest.mark.asyncio
async def test_auto_promote_high_score_advances() -> None:
    """experimental + score >= 0.5 + runs >= 5 → shadow."""
    registry = ProtocolRegistry(InMemoryProtocolStorage())
    proto = Protocol(
        protocol_id="x.test",
        version="1.0.0",
        tenant_id="u-test",
        status="experimental",
        trigger=ProtocolTrigger(task_type_pattern="x.*"),
        execution=ProtocolExecution(),
        metadata={"darwin_best_score": 0.8, "runs": 10},
    )
    await registry.save(proto)
    app = SimpleNamespace(state=SimpleNamespace(protocol_registry=registry))
    result = await auto_promote_protocols(app, "u-test")
    assert result["promoted"] == 1
    listed = await registry.list_all("u-test")
    assert listed[0].status == "shadow"


@pytest.mark.asyncio
async def test_auto_promote_disabled_via_env() -> None:
    app = SimpleNamespace(state=SimpleNamespace(protocol_registry=None))
    with patch.dict(os.environ, {"KUN_PROTOCOL_AUTO_PROMOTE_ENABLED": "0"}):
        result = await auto_promote_protocols(app, "u-test")
    assert result["skipped"] is True


# ===== AntiGaming Learner =====


def test_anti_gaming_learner_records_negative() -> None:
    reset_anti_gaming_learner()
    learner = get_anti_gaming_learner()
    learner.record_negative_feedback("u-test", "LLM 偷懒套话", "tk-1", rating=2)
    learner.record_negative_feedback("u-test", "LLM 偷懒套话", "tk-2", rating=1)
    patterns = learner.top_patterns("u-test")
    assert len(patterns) == 1
    assert patterns[0].count == 2
    assert "tk-1" in patterns[0].examples
    assert "tk-2" in patterns[0].examples


def test_anti_gaming_learner_ignores_positive() -> None:
    reset_anti_gaming_learner()
    learner = get_anti_gaming_learner()
    learner.record_negative_feedback("u-test", "good", "tk-1", rating=5)
    assert learner.top_patterns("u-test") == []


def test_anti_gaming_learner_singleton() -> None:
    reset_anti_gaming_learner()
    a = get_anti_gaming_learner()
    b = get_anti_gaming_learner()
    assert a is b


# ===== Verification Auto Gen =====


def test_verification_auto_gen_below_threshold() -> None:
    reset_verification_auto_gen()
    gen = get_verification_auto_gen()
    gen.record(
        TaskOutcomeSample(
            task_type="x.y",
            answer_length=100,
            duration_sec=5.0,
            cost_usd=0.01,
            success=True,
        )
    )
    template = gen.suggest("x.y")
    assert template.suggested == []  # 1 sample < 5


def test_verification_auto_gen_with_samples() -> None:
    reset_verification_auto_gen()
    gen = get_verification_auto_gen()
    for length in [50, 60, 80, 100, 120, 150, 200]:
        gen.record(
            TaskOutcomeSample(
                task_type="x.y",
                answer_length=length,
                duration_sec=5.0,
                cost_usd=0.01,
                success=True,
            )
        )
    template = gen.suggest("x.y")
    assert template.sample_size == 7
    assert any(s["kind"] == "exact_output" for s in template.suggested)


def test_verification_auto_gen_disabled() -> None:
    reset_verification_auto_gen()
    gen = get_verification_auto_gen()
    for length in [50, 60, 80, 100, 120, 150, 200]:
        gen.record(
            TaskOutcomeSample(
                task_type="x.y", answer_length=length, duration_sec=1, cost_usd=0, success=True
            )
        )
    with patch.dict(os.environ, {"KUN_VERIFICATION_AUTO_GEN_ENABLED": "0"}):
        template = gen.suggest("x.y")
    assert template.suggested == []


# ===== Multi Window =====


def test_multi_window_default_3_windows() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_WINDOWS_JSON", None)
        os.environ.pop("KUN_QI_MULTI_WINDOWS_ENABLED", None)
        windows = get_active_windows()
    assert len(windows) >= 1


def test_multi_window_disabled_falls_back_single() -> None:
    with patch.dict(os.environ, {"KUN_QI_MULTI_WINDOWS_ENABLED": "0"}):
        windows = get_active_windows()
    assert len(windows) == 1


def test_multi_window_custom_json() -> None:
    with patch.dict(
        os.environ,
        {"KUN_QI_WINDOWS_JSON": '[{"start":1,"end":2},{"start":15,"end":16}]'},
    ):
        windows = get_active_windows()
    assert len(windows) == 2
    assert windows[0].start_hour == 1
    assert windows[1].start_hour == 15


def test_is_any_window_active() -> None:
    """At least testable as bool — actual coverage depends on hour."""
    result = is_any_window_active()
    assert isinstance(result, bool)


# ===== AntiGamingLearner singleton/reset =====


def test_anti_gaming_learner_class_direct() -> None:
    learner = AntiGamingLearner()
    learner.record_negative_feedback("u", "x", "tk-1", rating=1)
    assert len(learner.top_patterns("u")) == 1
    learner.reset()
    assert len(learner.top_patterns("u")) == 0


def test_verification_auto_gen_class_direct() -> None:
    gen = VerificationAutoGen()
    for i in range(5):
        gen.record(TaskOutcomeSample("y", 100 + i, 1, 0, True))
    t = gen.suggest("y")
    assert t.sample_size == 5
    gen.reset()
    assert gen.suggest("y").sample_size == 0
