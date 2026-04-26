"""LabRecipePrecipitationStep + KP bridge — 闭环 lab → §16.6 (Wire 24)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.engineering.precipitation import (
    AssetUpdate,
    KnowledgePrecipitation,
    PrecipitationEvent,
)
from kun.lab import (
    HIGH_CONFIDENCE_WIN_RATE,
    LabRecipePrecipitationStep,
    install_lab_kp_bridge,
    make_kp_adopter,
    reset_adoption_step,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_adoption_step()
    yield
    reset_adoption_step()
    # install_lab_kp_bridge 把 step 注入了 idle_batch._steps registry,
    # 测试间清理避免污染 (e.g. test_lab_adoption 的 list_steps 判断)
    from kun.engineering.idle_batch import _steps

    _steps.pop("lab_recipe_adoption", None)


def _make_payload(
    *,
    promotion_id: str = "prom-1",
    target_module: str = "execution_mode_classifier",
    win_rate: float = 0.85,
    strategy: str = "tier_top_low_temp",
    task_type: str = "ad",
    total_count: int = 12,
) -> dict[str, Any]:
    return {
        "promotion_id": promotion_id,
        "task_type": task_type,
        "strategy": strategy,
        "win_rate": win_rate,
        "total_count": total_count,
        "avg_score": 0.78,
        "avg_cost_usd": 0.04,
        "target_module": target_module,
    }


# ---- LabRecipePrecipitationStep.precipitate ----


@pytest.mark.asyncio
async def test_precipitate_produces_one_asset_update_per_event() -> None:
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(),
    )
    updates = await step.precipitate(event)

    assert len(updates) == 1
    u = updates[0]
    assert isinstance(u, AssetUpdate)
    assert u.update_kind == "update"
    assert u.payload["source"] == "kun_lab"
    assert u.payload["promotion_id"] == "prom-1"


@pytest.mark.asyncio
async def test_precipitate_classifier_target_maps_to_playbook() -> None:
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(target_module="execution_mode_classifier"),
    )
    [u] = await step.precipitate(event)
    assert u.asset_kind == "playbook"
    assert u.asset_ref == "execution_mode_classifier"


@pytest.mark.asyncio
async def test_precipitate_prompt_template_target_maps_to_playbook() -> None:
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(target_module="hermes_prompt_template"),
    )
    [u] = await step.precipitate(event)
    assert u.asset_kind == "playbook"


@pytest.mark.asyncio
async def test_precipitate_general_target_maps_to_rule() -> None:
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(target_module="general"),
    )
    [u] = await step.precipitate(event)
    assert u.asset_kind == "rule"


@pytest.mark.asyncio
async def test_precipitate_high_winrate_no_approval_required() -> None:
    """win_rate ≥ 0.8 → 高置信, requires_approval=False."""
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(win_rate=0.85),
    )
    [u] = await step.precipitate(event)
    assert u.requires_approval is False
    assert u.confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_precipitate_low_winrate_requires_approval() -> None:
    """win_rate < 0.8 → 要主仓库审批."""
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(win_rate=0.65),
    )
    [u] = await step.precipitate(event)
    assert u.requires_approval is True
    assert u.confidence == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_precipitate_recommended_change_includes_stats() -> None:
    step = LabRecipePrecipitationStep()
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="experiment.promoted",
        payload=_make_payload(strategy="my_strat", task_type="biz"),
    )
    [u] = await step.precipitate(event)
    change = u.payload["recommended_change"]
    assert change["strategy"] == "my_strat"
    assert change["task_type"] == "biz"
    assert change["stats"]["win_rate"] == pytest.approx(0.85)
    assert change["stats"]["total_count"] == 12


@pytest.mark.asyncio
async def test_precipitate_winrate_clamped_to_unit_interval() -> None:
    """win_rate 越界 → confidence 仍 [0,1]."""
    step = LabRecipePrecipitationStep()
    for bad_rate in (-0.5, 1.5, 2.0):
        event = PrecipitationEvent(
            event_id=f"ev-{bad_rate}",
            event_type="experiment.promoted",
            payload=_make_payload(win_rate=bad_rate),
        )
        [u] = await step.precipitate(event)
        assert 0.0 <= u.confidence <= 1.0


@pytest.mark.asyncio
async def test_precipitate_missing_target_falls_back_to_general() -> None:
    step = LabRecipePrecipitationStep()
    payload = _make_payload()
    payload.pop("target_module")
    event = PrecipitationEvent(
        event_id="ev-x",
        event_type="experiment.promoted",
        payload=payload,
    )
    [u] = await step.precipitate(event)
    assert u.asset_ref == "general"
    assert u.asset_kind == "rule"


def test_high_confidence_threshold_is_0_8() -> None:
    """常量 sanity — 防意外被改."""
    assert HIGH_CONFIDENCE_WIN_RATE == 0.8


# ---- make_kp_adopter 包装 ----


@pytest.mark.asyncio
async def test_kp_adopter_dispatches_event_to_kp() -> None:
    """adopter(payload) → KP.dispatch 收到 PrecipitationEvent."""
    kp = KnowledgePrecipitation()
    kp.register_step(LabRecipePrecipitationStep())

    captured_updates: list[AssetUpdate] = []

    async def fake_apply_hook(update):
        captured_updates.append(update)

    kp.register_asset_apply_hook(fake_apply_hook)

    adopter = make_kp_adopter(kp)
    payload = _make_payload(promotion_id="prom-K1")
    await adopter(payload)

    assert len(captured_updates) == 1
    assert captured_updates[0].payload["promotion_id"] == "prom-K1"


@pytest.mark.asyncio
async def test_kp_adopter_uses_promotion_id_as_event_id() -> None:
    """promotion_id 进 PrecipitationEvent.event_id (用于幂等 / 回查)."""
    kp = KnowledgePrecipitation()

    seen_events: list[PrecipitationEvent] = []

    class CaptureStep:
        source_event_type = "experiment.promoted"
        step_kind = "weight_tune"
        schedule = "realtime"

        async def precipitate(self, event, context=None):
            seen_events.append(event)
            return []

    kp.register_step(CaptureStep())  # type: ignore[arg-type]

    adopter = make_kp_adopter(kp)
    await adopter(_make_payload(promotion_id="ID-1234"))

    assert len(seen_events) == 1
    assert seen_events[0].event_id == "ID-1234"


@pytest.mark.asyncio
async def test_kp_adopter_missing_promotion_id_generates_event_id() -> None:
    """promotion_id 缺失 → adopter 自动 new_id, 不爆."""
    kp = KnowledgePrecipitation()

    seen_events: list[PrecipitationEvent] = []

    class CaptureStep:
        source_event_type = "experiment.promoted"
        step_kind = "weight_tune"
        schedule = "realtime"

        async def precipitate(self, event, context=None):
            seen_events.append(event)
            return []

    kp.register_step(CaptureStep())  # type: ignore[arg-type]

    adopter = make_kp_adopter(kp)
    payload = _make_payload()
    payload.pop("promotion_id")
    await adopter(payload)

    assert len(seen_events) == 1
    assert seen_events[0].event_id  # auto-generated, non-empty
    assert seen_events[0].event_id.startswith("ev-")


# ---- install_lab_kp_bridge 端到端 ----


@pytest.mark.asyncio
async def test_install_bridge_registers_step_and_adopter() -> None:
    """install_lab_kp_bridge → KP 有 step + idle_batch step 注入了 KP adopter."""
    from kun.engineering.idle_batch import get_step

    kp = KnowledgePrecipitation()
    bridge_step = install_lab_kp_bridge(kp)

    # KP 注册了
    assert bridge_step in kp._steps
    assert isinstance(bridge_step, LabRecipePrecipitationStep)

    # idle_batch step 装了
    adoption = get_step("lab_recipe_adoption")
    assert adoption is not None


@pytest.mark.asyncio
async def test_install_bridge_end_to_end_event_to_asset_update() -> None:
    """整条链路: idle_batch step.run → KP → AssetUpdate → asset_apply_hook."""
    kp = KnowledgePrecipitation()
    install_lab_kp_bridge(kp)

    captured: list[AssetUpdate] = []

    async def fake_hook(update):
        captured.append(update)

    kp.register_asset_apply_hook(fake_hook)

    # 模拟 idle_batch 拉到 1 条 experiment.promoted
    from datetime import UTC, datetime

    from kun.lab import get_adoption_step

    fake_event = {
        "event_id": "ev-bridge-1",
        "occurred_at": datetime.now(UTC),
        "payload": _make_payload(promotion_id="end2end-1"),
    }

    async def fake_fetcher(**_kwargs):
        return [fake_event]

    adoption = get_adoption_step()
    adoption._event_fetcher = fake_fetcher  # 测试注入

    result = await adoption.run(tenant_id="u-test")
    assert result["adopted"] == 1
    # 通过整条链路: payload 已变成 AssetUpdate, 被 hook 收到
    assert len(captured) == 1
    assert captured[0].payload["promotion_id"] == "end2end-1"
    assert captured[0].asset_kind == "playbook"  # execution_mode_classifier
