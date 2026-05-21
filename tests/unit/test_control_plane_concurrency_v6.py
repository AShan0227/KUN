from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kun.control_plane import (
    FileResourceLockStore,
    InMemoryResourceLockStore,
    WorkItem,
)


NOW = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)


def _work_item(work_item_id: str = "work-lock") -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        mission_id="msn-locks",
        task_plan_version="v1",
        type="execution",
        owner="kun",
    )


def test_file_resource_lock_store_blocks_conflicting_daemons_until_release(tmp_path) -> None:
    store = FileResourceLockStore(tmp_path / "resource-locks.json")
    item_a = _work_item("work-a")
    item_b = _work_item("work-b")

    first = store.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-a",
        daemon_id="daemon-a",
        worker_id="worker-a",
        work_item=item_a,
        now=NOW,
        ttl=timedelta(minutes=15),
    )
    second = store.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-b",
        daemon_id="daemon-b",
        worker_id="worker-b",
        work_item=item_b,
        now=NOW + timedelta(seconds=1),
        ttl=timedelta(minutes=15),
    )

    assert first.acquired is True
    assert second.acquired is False
    assert second.conflicts[0].holder_daemon_id == "daemon-a"
    assert second.conflicts[0].waiting_work_item_id == "work-b"

    released = store.release_holder("lease-a", now=NOW + timedelta(seconds=2))
    retried = store.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-b",
        daemon_id="daemon-b",
        worker_id="worker-b",
        work_item=item_b,
        now=NOW + timedelta(seconds=3),
        ttl=timedelta(minutes=15),
    )

    assert [lease.holder_id for lease in released] == ["lease-a"]
    assert retried.acquired is True


def test_resource_lock_store_expires_stale_leases() -> None:
    store = InMemoryResourceLockStore()
    item_a = _work_item("work-a")
    item_b = _work_item("work-b")
    store.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-a",
        daemon_id="daemon-a",
        worker_id="worker-a",
        work_item=item_a,
        now=NOW,
        ttl=timedelta(seconds=1),
    )

    acquisition = store.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-b",
        daemon_id="daemon-b",
        worker_id="worker-b",
        work_item=item_b,
        now=NOW + timedelta(seconds=2),
        ttl=timedelta(minutes=15),
    )

    assert acquisition.acquired is True
    assert acquisition.conflicts == []
