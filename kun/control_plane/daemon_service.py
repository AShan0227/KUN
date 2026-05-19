"""Service-manager installation helpers for the V6 Control Plane daemon."""

from __future__ import annotations

import plistlib
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DaemonServicePlatform = Literal["launchd", "systemd"]


class DaemonServiceInstallPlan(BaseModel):
    """Auditable service-manager payload for running the daemon across restarts."""

    model_config = ConfigDict(extra="forbid")

    platform: DaemonServicePlatform
    service_name: str
    install_path: str
    working_directory: str
    command: list[str]
    store_path: str
    state_path: str
    stdout_path: str
    stderr_path: str
    content: str
    start_command: list[str]
    stop_command: list[str]
    uninstall_command: list[str]
    notes: list[str] = Field(default_factory=list)


def build_daemon_service_install_plan(
    *,
    platform: DaemonServicePlatform,
    service_name: str = "com.kun.control-plane.v6",
    working_directory: str | Path = ".",
    install_path: str | Path | None = None,
    store_path: str | Path = ".kun-local/v6-control-plane.json",
    state_path: str | Path = ".kun-local/v6-daemon-service.json",
    stdout_path: str | Path = ".kun-local/logs/v6-daemon.out.log",
    stderr_path: str | Path = ".kun-local/logs/v6-daemon.err.log",
    poll_interval_sec: float = 30.0,
    max_work_items_per_tick: int = 10,
    idle_ticks_to_stop: int = 1,
    stale_heartbeat_after_sec: float = 900.0,
    environment: Mapping[str, str] | None = None,
) -> DaemonServiceInstallPlan:
    """Build launchd/systemd service content without mutating the host."""

    if poll_interval_sec < 0:
        raise ValueError("poll_interval_sec must be non-negative")
    if max_work_items_per_tick < 0:
        raise ValueError("max_work_items_per_tick must be non-negative")
    if idle_ticks_to_stop <= 0:
        raise ValueError("idle_ticks_to_stop must be positive")
    if stale_heartbeat_after_sec <= 0:
        raise ValueError("stale_heartbeat_after_sec must be positive")

    workdir = Path(working_directory).expanduser().resolve()
    resolved_store_path = _resolve_under_workdir(workdir, store_path)
    resolved_state_path = _resolve_under_workdir(workdir, state_path)
    resolved_stdout_path = _resolve_under_workdir(workdir, stdout_path)
    resolved_stderr_path = _resolve_under_workdir(workdir, stderr_path)
    command = [
        sys.executable,
        "-m",
        "kun.cli",
        "control-plane",
        "daemon-run",
        "--store-path",
        str(resolved_store_path),
        "--state-path",
        str(resolved_state_path),
        "--daemon-id",
        service_name,
        "--poll-interval-sec",
        str(poll_interval_sec),
        "--max-work-items-per-tick",
        str(max_work_items_per_tick),
        "--idle-ticks-to-stop",
        str(idle_ticks_to_stop),
        "--stale-heartbeat-after-sec",
        str(stale_heartbeat_after_sec),
    ]
    env = dict(environment or {})
    if platform == "launchd":
        resolved_install_path = Path(install_path).expanduser().resolve() if install_path else (
            Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
        )
        content = _launchd_plist(
            service_name=service_name,
            command=command,
            working_directory=workdir,
            stdout_path=resolved_stdout_path,
            stderr_path=resolved_stderr_path,
            environment=env,
        )
        start_command = ["launchctl", "bootstrap", "gui/$(id -u)", str(resolved_install_path)]
        stop_command = ["launchctl", "bootout", "gui/$(id -u)", str(resolved_install_path)]
        uninstall_command = ["rm", "-f", str(resolved_install_path)]
    elif platform == "systemd":
        unit_name = f"{service_name}.service" if not service_name.endswith(".service") else service_name
        resolved_install_path = Path(install_path).expanduser().resolve() if install_path else (
            Path.home() / ".config" / "systemd" / "user" / unit_name
        )
        content = _systemd_unit(
            service_name=service_name,
            command=command,
            working_directory=workdir,
            stdout_path=resolved_stdout_path,
            stderr_path=resolved_stderr_path,
            environment=env,
        )
        start_command = ["systemctl", "--user", "enable", "--now", unit_name]
        stop_command = ["systemctl", "--user", "stop", unit_name]
        uninstall_command = ["systemctl", "--user", "disable", unit_name]
    else:  # pragma: no cover - Literal protects callers
        raise ValueError(f"unsupported daemon service platform: {platform}")

    return DaemonServiceInstallPlan(
        platform=platform,
        service_name=service_name,
        install_path=str(resolved_install_path),
        working_directory=str(workdir),
        command=command,
        store_path=str(resolved_store_path),
        state_path=str(resolved_state_path),
        stdout_path=str(resolved_stdout_path),
        stderr_path=str(resolved_stderr_path),
        content=content,
        start_command=start_command,
        stop_command=stop_command,
        uninstall_command=uninstall_command,
        notes=[
            "The generated service runs the KUN-native daemon entrypoint.",
            "Stop requests should go through `kun control-plane daemon-stop` before service shutdown.",
            "The service keeps durable heartbeat and Control Plane state separate.",
        ],
    )


def materialize_daemon_service_install_plan(
    plan: DaemonServiceInstallPlan,
    *,
    overwrite: bool = False,
) -> Path:
    """Write the service file, refusing to overwrite unless explicitly allowed."""

    path = Path(plan.install_path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"daemon service file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.content, encoding="utf-8")
    return path


def _resolve_under_workdir(workdir: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return workdir / path


def _launchd_plist(
    *,
    service_name: str,
    command: Sequence[str],
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    environment: Mapping[str, str],
) -> str:
    payload = {
        "Label": service_name,
        "ProgramArguments": list(command),
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    if environment:
        payload["EnvironmentVariables"] = dict(environment)
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def _systemd_unit(
    *,
    service_name: str,
    command: Sequence[str],
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    environment: Mapping[str, str],
) -> str:
    lines = [
        "[Unit]",
        f"Description=KUN V6 Control Plane daemon ({service_name})",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={working_directory}",
        f"ExecStart={_join_command(command)}",
        "Restart=always",
        "RestartSec=5",
        f"StandardOutput=append:{stdout_path}",
        f"StandardError=append:{stderr_path}",
    ]
    for key, value in sorted(environment.items()):
        lines.append(f"Environment={shlex.quote(f'{key}={value}')}")
    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def _join_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


__all__ = [
    "DaemonServiceInstallPlan",
    "DaemonServicePlatform",
    "build_daemon_service_install_plan",
    "materialize_daemon_service_install_plan",
]
