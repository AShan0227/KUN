"""LabRecipeAdoptionStep — idle_batch 消费 experiment.promoted (Wire 23)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.engineering.idle_batch import get_step, list_steps
from kun.lab import (
    LabRecipeAdoptionStep,
    install_lab_adoption_step,
    reset_adoption_step,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_adoption_step()
    yield
    reset_adoption_step()
    # install_lab_adoption_step 在某些测试里把 step 注入了 idle_batch._steps,
    # 清理避免污染下个测试 (顺序依赖很烦)
    from kun.engineering.idle_batch import _steps

    _steps.pop("lab_recipe_adoption", None)


def _make_event(
    promotion_id: str,
    *,
    occurred_at: datetime | None = None,
    strategy: str = "tier_top_low_temp",
    task_type: str = "ad",
) -> dict[str, Any]:
    return {
        "event_id": f"ev-{promotion_id}",
        "occurred_at": occurred_at or datetime.now(UTC),
        "payload": {
            "promotion_id": promotion_id,
            "task_type": task_type,
            "strategy": strategy,
            "win_rate": 0.85,
            "total_count": 12,
            "avg_score": 0.78,
            "avg_cost_usd": 0.04,
            "target_module": "execution_mode_classifier",
        },
    }


# ---- step.run 基础流程 ----


@pytest.mark.asyncio
async def test_adoption_no_events_returns_zero() -> None:
    """fetcher 返空 → scanned=0, adopted=0."""

    async def fake_fetcher(**_kwargs):
        return []

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher)
    result = await step.run(tenant_id="u-test")
    assert result["scanned"] == 0
    assert result["adopted"] == 0


@pytest.mark.asyncio
async def test_adoption_calls_adopter_for_each_event() -> None:
    """3 个新 promotion → adopter 被调 3 次, 拿到 payload."""
    captured: list[dict[str, Any]] = []

    async def fake_adopter(payload):
        captured.append(payload)

    events = [_make_event(f"prom-{i}") for i in range(3)]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(adopter=fake_adopter, event_fetcher=fake_fetcher)
    result = await step.run(tenant_id="u-test")

    assert result["scanned"] == 3
    assert result["adopted"] == 3
    assert result["errors"] == 0
    assert len(captured) == 3
    assert captured[0]["promotion_id"] == "prom-0"
    assert captured[2]["strategy"] == "tier_top_low_temp"


@pytest.mark.asyncio
async def test_adoption_skips_already_adopted() -> None:
    """同一 promotion_id 第二次 run → skipped, adopter 不再调."""
    call_count = 0

    async def fake_adopter(payload):
        nonlocal call_count
        call_count += 1

    events = [_make_event("prom-A"), _make_event("prom-B")]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(adopter=fake_adopter, event_fetcher=fake_fetcher)
    r1 = await step.run(tenant_id="u-test")
    r2 = await step.run(tenant_id="u-test")

    assert r1["adopted"] == 2
    assert r2["adopted"] == 0
    assert r2["skipped"] == 2
    assert call_count == 2  # 第二次 0 调用


@pytest.mark.asyncio
async def test_adoption_adopter_failure_counted_but_doesnt_break_others() -> None:
    """1 条 adopter 抛 → errors=1, 其他仍 adopt."""
    counter = {"good": 0, "bad": 0}

    async def fake_adopter(payload):
        if payload["promotion_id"] == "bad":
            counter["bad"] += 1
            raise RuntimeError("simulated downstream error")
        counter["good"] += 1

    events = [_make_event("good-1"), _make_event("bad"), _make_event("good-2")]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(adopter=fake_adopter, event_fetcher=fake_fetcher)
    result = await step.run(tenant_id="u-test")

    assert result["scanned"] == 3
    assert result["adopted"] == 2
    assert result["errors"] == 1
    assert counter == {"good": 2, "bad": 1}


@pytest.mark.asyncio
async def test_adoption_max_per_cycle_caps_processed() -> None:
    """fetcher 返很多, max_per_cycle 限制每轮上限."""
    captured: list[dict[str, Any]] = []

    async def fake_adopter(payload):
        captured.append(payload)

    events = [_make_event(f"p-{i}") for i in range(20)]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(
        adopter=fake_adopter, event_fetcher=fake_fetcher, max_per_cycle=5
    )
    result = await step.run(tenant_id="u-test")

    assert result["scanned"] == 5
    assert len(captured) == 5


@pytest.mark.asyncio
async def test_adoption_advances_cursor() -> None:
    """run 后 last_adopted_at = max(events.occurred_at)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        _make_event("p-1", occurred_at=base),
        _make_event("p-2", occurred_at=base + timedelta(hours=1)),
        _make_event("p-3", occurred_at=base + timedelta(hours=2)),
    ]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher)
    await step.run(tenant_id="u-test")

    assert step.state.last_adopted_at == base + timedelta(hours=2)
    assert step.state.adopted_promotion_ids == {"p-1", "p-2", "p-3"}


@pytest.mark.asyncio
async def test_adoption_fetcher_receives_cursor() -> None:
    """fetcher 能拿到 since= cursor, 跑第二轮时 cursor 已推进."""
    captured_kwargs: list[dict[str, Any]] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    call_count = 0

    async def fake_fetcher(**kwargs):
        nonlocal call_count
        captured_kwargs.append(kwargs)
        call_count += 1
        if call_count == 1:
            return [_make_event("p-1", occurred_at=base + timedelta(hours=1))]
        return []  # second call: nothing new

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher)
    await step.run(tenant_id="u-test")
    await step.run(tenant_id="u-test")

    assert len(captured_kwargs) == 2
    # 第一次 since = 0 timestamp
    assert captured_kwargs[0]["since"] == datetime.fromtimestamp(0, tz=UTC)
    # 第二次 since 推进到 base+1h
    assert captured_kwargs[1]["since"] == base + timedelta(hours=1)


@pytest.mark.asyncio
async def test_adoption_default_adopter_does_not_raise() -> None:
    """没注入 adopter → 默认 noop log, 不爆."""
    events = [_make_event("p-default")]

    async def fake_fetcher(**_kwargs):
        return events

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher)
    result = await step.run(tenant_id="u-test")
    assert result["adopted"] == 1


@pytest.mark.asyncio
async def test_adoption_default_fetcher_no_db_returns_empty() -> None:
    """没注入 fetcher + 没 DB 配置 → 默认 fetcher 静默返空 (不爆)."""
    step = LabRecipeAdoptionStep()  # 无注入
    result = await step.run(tenant_id="u-test")
    assert result["scanned"] == 0
    assert result["adopted"] == 0


# ---- registry 集成 ----


def test_install_registers_step_in_idle_batch() -> None:
    """install_lab_adoption_step → step 出现在 idle_batch.list_steps()."""
    assert "lab_recipe_adoption" not in list_steps()
    step = install_lab_adoption_step()
    assert "lab_recipe_adoption" in list_steps()
    assert get_step("lab_recipe_adoption") is step


@pytest.mark.asyncio
async def test_install_passes_through_adopter_and_fetcher() -> None:
    """install_lab_adoption_step(adopter=..., fetcher=...) → 真用进 step."""
    captured: list[dict[str, Any]] = []

    async def fake_adopter(payload):
        captured.append(payload)

    async def fake_fetcher(**_kwargs):
        return [_make_event("install-1")]

    step = install_lab_adoption_step(adopter=fake_adopter, event_fetcher=fake_fetcher)
    result = await step.run(tenant_id="u-test")

    assert result["adopted"] == 1
    assert captured[0]["promotion_id"] == "install-1"


def test_get_adoption_step_returns_singleton() -> None:
    from kun.lab import get_adoption_step

    a = get_adoption_step()
    b = get_adoption_step()
    assert a is b


def test_step_reset_clears_cursor_and_set() -> None:
    step = install_lab_adoption_step()
    step.state.adopted_promotion_ids.add("test-1")
    step.state.last_adopted_at = datetime(2026, 6, 1, tzinfo=UTC)
    step.reset()
    assert step.state.adopted_promotion_ids == set()
    assert step.state.last_adopted_at == datetime.fromtimestamp(0, tz=UTC)
