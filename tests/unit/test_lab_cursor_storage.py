"""LabRecipeAdoptionStep cursor 持久化 (Wire 29B)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.lab import (
    CursorSnapshot,
    InMemoryCursorStorage,
    LabRecipeAdoptionStep,
)


def _ev(promotion_id: str, *, occurred_at: datetime | None = None) -> dict[str, Any]:
    return {
        "event_id": f"ev-{promotion_id}",
        "occurred_at": occurred_at or datetime.now(UTC),
        "payload": {
            "promotion_id": promotion_id,
            "task_type": "ad",
            "strategy": "tier_top_low_temp",
            "win_rate": 0.85,
            "target_module": "execution_mode_classifier",
        },
    }


# ---- CursorSnapshot ----


def test_snapshot_empty_starts_at_epoch() -> None:
    s = CursorSnapshot.empty()
    assert s.last_adopted_at == datetime.fromtimestamp(0, tz=UTC)
    assert s.adopted_promotion_ids == []


# ---- InMemoryCursorStorage ----


@pytest.mark.asyncio
async def test_inmem_load_empty_returns_empty_snapshot() -> None:
    storage = InMemoryCursorStorage()
    s = await storage.load("default")
    assert s.last_adopted_at == datetime.fromtimestamp(0, tz=UTC)
    assert s.adopted_promotion_ids == []


@pytest.mark.asyncio
async def test_inmem_save_then_load_round_trip() -> None:
    storage = InMemoryCursorStorage()
    base = datetime(2026, 5, 1, tzinfo=UTC)
    snap = CursorSnapshot(last_adopted_at=base, adopted_promotion_ids=["p-1", "p-2", "p-3"])
    await storage.save("default", snap)
    loaded = await storage.load("default")
    assert loaded.last_adopted_at == base
    assert loaded.adopted_promotion_ids == ["p-1", "p-2", "p-3"]


@pytest.mark.asyncio
async def test_inmem_save_copies_ids_no_external_mutation() -> None:
    storage = InMemoryCursorStorage()
    ids = ["p-1"]
    snap = CursorSnapshot(last_adopted_at=datetime.now(UTC), adopted_promotion_ids=ids)
    await storage.save("default", snap)
    ids.append("p-2")  # 外部 mutate
    loaded = await storage.load("default")
    assert loaded.adopted_promotion_ids == ["p-1"]  # 不受外部影响


@pytest.mark.asyncio
async def test_inmem_isolated_by_cursor_name() -> None:
    storage = InMemoryCursorStorage()
    base = datetime(2026, 5, 1, tzinfo=UTC)
    await storage.save(
        "tenant-A",
        CursorSnapshot(last_adopted_at=base, adopted_promotion_ids=["A-1"]),
    )
    await storage.save(
        "tenant-B",
        CursorSnapshot(last_adopted_at=base, adopted_promotion_ids=["B-1"]),
    )
    a = await storage.load("tenant-A")
    b = await storage.load("tenant-B")
    assert a.adopted_promotion_ids == ["A-1"]
    assert b.adopted_promotion_ids == ["B-1"]


# ---- LabRecipeAdoptionStep 集成 ----


@pytest.mark.asyncio
async def test_step_loads_cursor_on_first_run() -> None:
    """模拟重启场景: 提前 save 的 cursor 应该被 load 走."""
    storage = InMemoryCursorStorage()
    base = datetime(2026, 5, 1, tzinfo=UTC)
    await storage.save(
        "default",
        CursorSnapshot(
            last_adopted_at=base,
            adopted_promotion_ids=["already-adopted-p1", "already-adopted-p2"],
        ),
    )

    captured_since: list[datetime] = []

    async def fake_fetcher(*, since, **_kwargs):
        captured_since.append(since)
        return []  # nothing new

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher, cursor_storage=storage)
    await step.run(tenant_id="u-test")

    # state 已从 storage load
    assert step.state.last_adopted_at == base
    assert "already-adopted-p1" in step.state.adopted_promotion_ids
    # fetcher 拿到正确的 since cursor (而非 epoch)
    assert captured_since[0] == base


@pytest.mark.asyncio
async def test_step_persists_cursor_after_run() -> None:
    """run 完 cursor 应该 save 进 storage."""
    storage = InMemoryCursorStorage()
    base = datetime(2026, 5, 1, tzinfo=UTC)

    async def fake_fetcher(**_kwargs):
        return [
            _ev("new-1", occurred_at=base),
            _ev("new-2", occurred_at=base + timedelta(hours=1)),
        ]

    step = LabRecipeAdoptionStep(event_fetcher=fake_fetcher, cursor_storage=storage)
    await step.run(tenant_id="u-test")

    # storage 应该有持久化的 cursor
    snap = await storage.load("default")
    assert snap.last_adopted_at == base + timedelta(hours=1)
    assert "new-1" in snap.adopted_promotion_ids
    assert "new-2" in snap.adopted_promotion_ids


@pytest.mark.asyncio
async def test_step_simulated_restart_skips_already_adopted() -> None:
    """端到端: step1 跑完 → step2 (新 instance, 同 storage) 不重复 adopt."""
    storage = InMemoryCursorStorage()

    captured_calls: list[str] = []

    async def fake_adopter(payload):
        captured_calls.append(payload["promotion_id"])

    base = datetime(2026, 5, 1, tzinfo=UTC)
    events_round1 = [
        _ev("p-A", occurred_at=base),
        _ev("p-B", occurred_at=base + timedelta(hours=1)),
    ]

    async def fetcher_round1(**_kwargs):
        return events_round1

    step1 = LabRecipeAdoptionStep(
        adopter=fake_adopter, event_fetcher=fetcher_round1, cursor_storage=storage
    )
    r1 = await step1.run(tenant_id="u-test")
    assert r1["adopted"] == 2

    # "重启": 新 step instance, 同 storage. 第二轮 fetcher 还返同样的事件 (e.g. NATS 重投)
    captured_calls.clear()
    step2 = LabRecipeAdoptionStep(
        adopter=fake_adopter, event_fetcher=fetcher_round1, cursor_storage=storage
    )
    r2 = await step2.run(tenant_id="u-test")
    # cursor 已 load → adopted_ids set 已含 p-A/p-B → skipped
    assert r2["adopted"] == 0
    assert r2["skipped"] == 2
    assert captured_calls == []  # adopter 没再被调


@pytest.mark.asyncio
async def test_step_per_cursor_name_isolation() -> None:
    """两个 step 用不同 cursor_name → 独立 cursor, 互不污染."""
    storage = InMemoryCursorStorage()

    async def fetcher(**_kwargs):
        return [_ev("p-shared")]

    captured_a: list[str] = []
    captured_b: list[str] = []

    async def adopter_a(payload):
        captured_a.append(payload["promotion_id"])

    async def adopter_b(payload):
        captured_b.append(payload["promotion_id"])

    step_a = LabRecipeAdoptionStep(
        adopter=adopter_a,
        event_fetcher=fetcher,
        cursor_storage=storage,
        cursor_name="tenant-A",
    )
    step_b = LabRecipeAdoptionStep(
        adopter=adopter_b,
        event_fetcher=fetcher,
        cursor_storage=storage,
        cursor_name="tenant-B",
    )

    await step_a.run(tenant_id="u-A")
    await step_b.run(tenant_id="u-B")

    # 两个都各 adopt 一次 (cursor 隔离)
    assert captured_a == ["p-shared"]
    assert captured_b == ["p-shared"]


@pytest.mark.asyncio
async def test_step_reset_clears_in_memory_but_keeps_storage() -> None:
    """reset 只清 in-memory, storage 持久化数据保留."""
    storage = InMemoryCursorStorage()
    base = datetime(2026, 5, 1, tzinfo=UTC)

    async def fetcher(**_kwargs):
        return [_ev("p-keep", occurred_at=base)]

    step = LabRecipeAdoptionStep(event_fetcher=fetcher, cursor_storage=storage)
    await step.run(tenant_id="u-test")

    # 持久化保留
    snap = await storage.load("default")
    assert "p-keep" in snap.adopted_promotion_ids

    step.reset()
    # in-memory state 清空
    assert step.state.adopted_promotion_ids == set()
    # storage 仍然有
    snap_after_reset = await storage.load("default")
    assert "p-keep" in snap_after_reset.adopted_promotion_ids


@pytest.mark.asyncio
async def test_step_storage_load_failure_falls_back_empty() -> None:
    """storage.load 抛异常 → fallback empty cursor, 不爆."""

    class FailingStorage:
        async def load(self, name):
            raise RuntimeError("simulated load failure")

        async def save(self, name, snap):
            pass

    async def fetcher(**_kwargs):
        return []

    step = LabRecipeAdoptionStep(event_fetcher=fetcher, cursor_storage=FailingStorage())
    result = await step.run(tenant_id="u-test")
    assert result["scanned"] == 0
    # state 是 empty cursor
    assert step.state.last_adopted_at == datetime.fromtimestamp(0, tz=UTC)


@pytest.mark.asyncio
async def test_step_storage_save_failure_doesnt_break_run() -> None:
    """storage.save 抛异常 → run 仍正常返, 只 log."""

    class SaveFailingStorage:
        async def load(self, name):
            from kun.lab.cursor_storage import CursorSnapshot

            return CursorSnapshot.empty()

        async def save(self, name, snap):
            raise RuntimeError("simulated save failure")

    async def fetcher(**_kwargs):
        return [_ev("p-1")]

    step = LabRecipeAdoptionStep(event_fetcher=fetcher, cursor_storage=SaveFailingStorage())
    result = await step.run(tenant_id="u-test")
    assert result["adopted"] == 1  # adopt 成功 (in-memory)
