from __future__ import annotations

import plistlib

import pytest
from kun.control_plane import (
    build_daemon_service_install_plan,
    build_daemon_worker_pool_service_install_plans,
    materialize_daemon_service_install_plan,
    materialize_daemon_worker_pool_service_plan,
)


def test_launchd_daemon_service_plan_contains_persistent_daemon_command(tmp_path) -> None:
    plan = build_daemon_service_install_plan(
        platform="launchd",
        service_name="com.kun.control-plane.test",
        working_directory=tmp_path,
        install_path=tmp_path / "com.kun.control-plane.test.plist",
        poll_interval_sec=5,
    )

    payload = plistlib.loads(plan.content.encode("utf-8"))
    assert payload["Label"] == "com.kun.control-plane.test"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    shell_command = payload["ProgramArguments"][2]
    assert "control-plane daemon-run" in shell_command
    assert "--store-path" in shell_command
    assert "--state-path" in shell_command
    assert "--max-ticks" not in shell_command
    assert "--keep-running-when-idle" in shell_command
    assert "/Library/Logs/KUN/" in payload["StandardOutPath"]
    assert "/Library/Logs/KUN/" in payload["StandardErrorPath"]
    assert plan.start_command[:2] == ["launchctl", "bootstrap"]
    assert plan.stop_command[:2] == ["launchctl", "bootout"]


def test_daemon_service_plan_can_embed_ab_regression_round(tmp_path) -> None:
    ab_round_dir = tmp_path / "ab-round"
    plan = build_daemon_service_install_plan(
        platform="launchd",
        service_name="com.kun.control-plane.test",
        working_directory=tmp_path,
        install_path=tmp_path / "com.kun.control-plane.test.plist",
        ab_round_dir=ab_round_dir,
        ab_round_id="round-02-regression",
    )

    payload = plistlib.loads(plan.content.encode("utf-8"))
    shell_command = payload["ProgramArguments"][2]
    assert "--ab-round-dir" in shell_command
    assert str(ab_round_dir) in shell_command
    assert "--ab-round-id" in shell_command
    assert "round-02-regression" in shell_command


def test_daemon_service_plan_can_use_redis_resource_lock_backend(tmp_path) -> None:
    plan = build_daemon_service_install_plan(
        platform="launchd",
        service_name="com.kun.control-plane.redis-test",
        working_directory=tmp_path,
        install_path=tmp_path / "com.kun.control-plane.redis-test.plist",
        resource_lock_backend="redis",
        resource_lock_redis_url="redis://localhost:6379/0",
    )

    payload = plistlib.loads(plan.content.encode("utf-8"))
    shell_command = payload["ProgramArguments"][2]
    assert "--resource-lock-backend redis" in shell_command
    assert "--resource-lock-redis-url redis://localhost:6379/0" in shell_command


def test_systemd_daemon_service_plan_uses_restart_policy(tmp_path) -> None:
    plan = build_daemon_service_install_plan(
        platform="systemd",
        service_name="kun-control-plane-v6",
        working_directory=tmp_path,
        install_path=tmp_path / "kun-control-plane-v6.service",
        environment={"KUN_ENV": "test"},
    )

    assert "[Service]" in plan.content
    assert "ExecStart=" in plan.content
    assert "control-plane daemon-run" in plan.content
    assert "Restart=always" in plan.content
    assert "Environment=KUN_ENV=test" in plan.content
    assert plan.start_command == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "kun-control-plane-v6.service",
    ]


def test_materialize_daemon_service_install_plan_refuses_accidental_overwrite(
    tmp_path,
) -> None:
    plan = build_daemon_service_install_plan(
        platform="systemd",
        service_name="kun-control-plane-v6",
        working_directory=tmp_path,
        install_path=tmp_path / "kun-control-plane-v6.service",
    )

    written = materialize_daemon_service_install_plan(plan)
    assert written.read_text(encoding="utf-8") == plan.content
    assert (tmp_path / "kun-control-plane-v6.service").parent.exists()
    assert (tmp_path / ".kun-local" / "logs").exists()
    with pytest.raises(FileExistsError):
        materialize_daemon_service_install_plan(plan)
    materialize_daemon_service_install_plan(plan, overwrite=True)


def test_worker_pool_service_plan_uses_distinct_daemons_and_shared_sqlite_lock(
    tmp_path,
) -> None:
    plan = build_daemon_worker_pool_service_install_plans(
        platform="systemd",
        service_name="kun-control-plane-v6",
        fleet_id="fleet-test",
        replica_count=3,
        working_directory=tmp_path,
        install_path=tmp_path / "kun-control-plane-v6.service",
        store_path=tmp_path / "control-plane.json",
        state_path=tmp_path / "daemon-state.json",
        resource_lock_backend="sqlite",
    )

    assert plan.replica_count == 3
    assert plan.resource_lock_backend == "sqlite"
    assert len({replica.service_name for replica in plan.plans}) == 3
    assert len({replica.state_path for replica in plan.plans}) == 3
    assert {replica.store_path for replica in plan.plans} == {str(tmp_path / "control-plane.json")}
    assert len({replica.resource_lock_path for replica in plan.plans}) == 1
    assert all("--resource-lock-backend sqlite" in replica.content for replica in plan.plans)

    written = materialize_daemon_worker_pool_service_plan(plan)
    assert len(written) == 3
    assert all(path.exists() for path in written)
