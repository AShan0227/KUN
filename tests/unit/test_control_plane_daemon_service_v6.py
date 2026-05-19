from __future__ import annotations

import plistlib

import pytest
from kun.control_plane import (
    build_daemon_service_install_plan,
    materialize_daemon_service_install_plan,
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
    assert "control-plane" in payload["ProgramArguments"]
    assert "daemon-run" in payload["ProgramArguments"]
    assert "--store-path" in payload["ProgramArguments"]
    assert "--state-path" in payload["ProgramArguments"]
    assert "--max-ticks" not in payload["ProgramArguments"]
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
    assert "--ab-round-dir" in payload["ProgramArguments"]
    assert str(ab_round_dir) in payload["ProgramArguments"]
    assert "--ab-round-id" in payload["ProgramArguments"]
    assert "round-02-regression" in payload["ProgramArguments"]


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
    with pytest.raises(FileExistsError):
        materialize_daemon_service_install_plan(plan)
    materialize_daemon_service_install_plan(plan, overwrite=True)
