from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kun.control_plane import (
    FileResourceLockStore,
    InMemoryResourceLockStore,
    RedisResourceLockStore,
    SQLiteResourceLockStore,
    WorkItem,
    normalize_resource_lock_ref,
)

NOW = datetime(2026, 5, 21, 9, 0, tzinfo=UTC)


class FakeRedisPipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str, str | None]] = []

    def __enter__(self) -> FakeRedisPipeline:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def watch(self, *_keys: str) -> None:
        return None

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.redis.values.get(key) for key in keys]

    def unwatch(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str, *, px: int) -> None:
        self.commands.append(("set", key, value))

    def execute(self) -> list[bool]:
        for _command, key, value in self.commands:
            if value is not None:
                self.redis.values[key] = value
        return [True for _ in self.commands]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def scan_iter(self, pattern: str):
        prefix = pattern.removesuffix("*")
        return (key for key in list(self.values) if key.startswith(prefix))

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


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


def test_resource_lock_store_normalizes_workspace_uri_and_path_refs(tmp_path) -> None:
    workspace = tmp_path / "project"
    store = InMemoryResourceLockStore()
    item_a = _work_item("work-a")
    item_b = _work_item("work-b")

    first = store.acquire_many(
        resources=[f"workspace:workspace://{workspace}"],
        holder_id="lease-a",
        daemon_id="daemon-a",
        worker_id="worker-a",
        work_item=item_a,
        now=NOW,
        ttl=timedelta(minutes=15),
    )
    second = store.acquire_many(
        resources=[f"workspace:{workspace}"],
        holder_id="lease-b",
        daemon_id="daemon-b",
        worker_id="worker-b",
        work_item=item_b,
        now=NOW + timedelta(seconds=1),
        ttl=timedelta(minutes=15),
    )

    assert first.acquired is True
    assert second.acquired is False
    assert second.conflicts[0].resource_ref == normalize_resource_lock_ref(f"workspace:{workspace}")


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


def test_sqlite_resource_lock_store_coordinates_independent_processes(tmp_path) -> None:
    path = tmp_path / "resource-locks.sqlite3"
    store_a = SQLiteResourceLockStore(path)
    store_b = SQLiteResourceLockStore(path)
    item_a = _work_item("work-a")
    item_b = _work_item("work-b")

    first = store_a.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-a",
        daemon_id="daemon-a",
        worker_id="worker-a",
        work_item=item_a,
        now=NOW,
        ttl=timedelta(minutes=15),
    )
    second = store_b.acquire_many(
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

    store_a.release_holder("lease-a", now=NOW + timedelta(seconds=2))
    retried = store_b.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-b",
        daemon_id="daemon-b",
        worker_id="worker-b",
        work_item=item_b,
        now=NOW + timedelta(seconds=3),
        ttl=timedelta(minutes=15),
    )

    assert retried.acquired is True


def test_redis_resource_lock_store_blocks_cross_machine_conflicts() -> None:
    fake_redis = FakeRedis()
    store_a = RedisResourceLockStore(client=fake_redis)
    store_b = RedisResourceLockStore(client=fake_redis)
    item_a = _work_item("work-a")
    item_b = _work_item("work-b")

    first = store_a.acquire_many(
        resources=["workspace:/repo"],
        holder_id="lease-a",
        daemon_id="daemon-a",
        worker_id="worker-a",
        work_item=item_a,
        now=NOW,
        ttl=timedelta(minutes=15),
    )
    second = store_b.acquire_many(
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

    released = store_a.release_holder("lease-a", now=NOW + timedelta(seconds=2))
    retried = store_b.acquire_many(
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
