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
    resource_lock_path: str
    resource_lock_backend: str = "file"
    resource_lock_redis_url: str | None = None
    stdout_path: str
    stderr_path: str
    content: str
    start_command: list[str]
    stop_command: list[str]
    uninstall_command: list[str]
    notes: list[str] = Field(default_factory=list)


class DaemonWorkerPoolServicePlan(BaseModel):
    """A multi-process worker-pool service plan made of independent daemons."""

    model_config = ConfigDict(extra="forbid")

    platform: DaemonServicePlatform
    fleet_id: str
    replica_count: int
    per_process_worker_pool_size: int
    shared_store_path: str
    shared_resource_lock_path: str
    resource_lock_backend: str
    plans: list[DaemonServiceInstallPlan]
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
    worker_pool_size: int = 1,
    resource_lock_path: str | Path | None = None,
    resource_lock_backend: str = "file",
    resource_lock_redis_url: str | None = None,
    resource_lock_ttl_sec: float = 900.0,
    sandbox_mode: str = "workspace_snapshot",
    container_runtime: str | None = None,
    idle_ticks_to_stop: int = 1,
    stale_heartbeat_after_sec: float = 900.0,
    ab_round_dir: str | Path | None = None,
    ab_round_id: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> DaemonServiceInstallPlan:
    """Build launchd/systemd service content without mutating the host."""

    if poll_interval_sec < 0:
        raise ValueError("poll_interval_sec must be non-negative")
    if max_work_items_per_tick < 0:
        raise ValueError("max_work_items_per_tick must be non-negative")
    if worker_pool_size <= 0:
        raise ValueError("worker_pool_size must be positive")
    if idle_ticks_to_stop <= 0:
        raise ValueError("idle_ticks_to_stop must be positive")
    if stale_heartbeat_after_sec <= 0:
        raise ValueError("stale_heartbeat_after_sec must be positive")
    if resource_lock_backend not in {"file", "sqlite", "redis"}:
        raise ValueError("resource_lock_backend must be file, sqlite, or redis")
    if resource_lock_backend == "redis" and not resource_lock_redis_url:
        raise ValueError("resource_lock_redis_url is required for redis backend")

    workdir = Path(working_directory).expanduser().resolve()
    resolved_store_path = _resolve_under_workdir(workdir, store_path)
    resolved_state_path = _resolve_under_workdir(workdir, state_path)
    resolved_resource_lock_path = (
        _resolve_under_workdir(workdir, resource_lock_path)
        if resource_lock_path is not None
        else resolved_store_path.with_name(
            f"{resolved_store_path.stem}.resource-locks."
            f"{'sqlite3' if resource_lock_backend == 'sqlite' else 'json'}"
        )
    )
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
        "--worker-pool-size",
        str(worker_pool_size),
        "--resource-lock-path",
        str(resolved_resource_lock_path),
        "--resource-lock-backend",
        resource_lock_backend,
        "--resource-lock-ttl-sec",
        str(resource_lock_ttl_sec),
        "--sandbox-mode",
        sandbox_mode,
        "--keep-running-when-idle",
        "--idle-ticks-to-stop",
        str(idle_ticks_to_stop),
        "--stale-heartbeat-after-sec",
        str(stale_heartbeat_after_sec),
    ]
    if resource_lock_redis_url:
        command.extend(["--resource-lock-redis-url", resource_lock_redis_url])
    if container_runtime:
        command.extend(["--container-runtime", container_runtime])
    if ab_round_dir is not None:
        command.extend(["--ab-round-dir", str(_resolve_under_workdir(workdir, ab_round_dir))])
    if ab_round_id:
        command.extend(["--ab-round-id", ab_round_id])
    env = dict(environment or {})
    if platform == "launchd":
        log_dir = Path.home() / "Library" / "Logs" / "KUN"
        safe_service_name = service_name.replace("/", "_")
        resolved_stdout_path = log_dir / f"{safe_service_name}.out.log"
        resolved_stderr_path = log_dir / f"{safe_service_name}.err.log"
        resolved_install_path = (
            Path(install_path).expanduser().resolve()
            if install_path
            else (Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist")
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
        unit_name = (
            f"{service_name}.service" if not service_name.endswith(".service") else service_name
        )
        resolved_install_path = (
            Path(install_path).expanduser().resolve()
            if install_path
            else (Path.home() / ".config" / "systemd" / "user" / unit_name)
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
        resource_lock_path=str(resolved_resource_lock_path),
        resource_lock_backend=resource_lock_backend,
        resource_lock_redis_url=resource_lock_redis_url,
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
            "For multi-process pools, use one state file per daemon and a shared store/resource lock.",
        ],
    )


def build_daemon_worker_pool_service_install_plans(
    *,
    platform: DaemonServicePlatform,
    fleet_id: str = "kun-control-plane-worker-pool",
    service_name: str = "com.kun.control-plane.v6",
    replica_count: int = 2,
    per_process_worker_pool_size: int = 1,
    working_directory: str | Path = ".",
    install_path: str | Path | None = None,
    store_path: str | Path = ".kun-local/v6-control-plane.json",
    state_path: str | Path = ".kun-local/v6-daemon-service.json",
    stdout_path: str | Path = ".kun-local/logs/v6-daemon.out.log",
    stderr_path: str | Path = ".kun-local/logs/v6-daemon.err.log",
    poll_interval_sec: float = 30.0,
    max_work_items_per_tick: int = 10,
    resource_lock_path: str | Path | None = None,
    resource_lock_backend: str = "sqlite",
    resource_lock_redis_url: str | None = None,
    resource_lock_ttl_sec: float = 900.0,
    sandbox_mode: str = "workspace_snapshot",
    container_runtime: str | None = None,
    idle_ticks_to_stop: int = 1,
    stale_heartbeat_after_sec: float = 900.0,
    ab_round_dir: str | Path | None = None,
    ab_round_id: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> DaemonWorkerPoolServicePlan:
    """Build a real multi-process daemon pool plan.

    Every replica has its own service/state/log identity, while sharing the
    Control Plane store and resource-lock database.  This is the local-first
    production path before moving to a remote queue or Kubernetes scheduler.
    """

    if replica_count <= 0:
        raise ValueError("replica_count must be positive")
    if per_process_worker_pool_size <= 0:
        raise ValueError("per_process_worker_pool_size must be positive")
    workdir = Path(working_directory).expanduser().resolve()
    shared_store_path = _resolve_under_workdir(workdir, store_path)
    shared_resource_lock_path = (
        _resolve_under_workdir(workdir, resource_lock_path)
        if resource_lock_path is not None
        else shared_store_path.with_name(f"{shared_store_path.stem}.resource-locks.sqlite3")
    )
    plans: list[DaemonServiceInstallPlan] = []
    service_default_ext = ".service" if platform == "systemd" else ".plist"
    for index in range(1, replica_count + 1):
        suffix = f"worker-{index}"
        replica_service_name = f"{service_name}.{suffix}"
        replica_install_path = (
            _replica_path(install_path, suffix=suffix, default_ext=service_default_ext)
            if install_path is not None
            else None
        )
        plans.append(
            build_daemon_service_install_plan(
                platform=platform,
                service_name=replica_service_name,
                working_directory=workdir,
                install_path=replica_install_path,
                store_path=shared_store_path,
                state_path=_replica_path(state_path, suffix=suffix, default_ext=".json"),
                stdout_path=_replica_path(stdout_path, suffix=suffix, default_ext=".log"),
                stderr_path=_replica_path(stderr_path, suffix=suffix, default_ext=".log"),
                poll_interval_sec=poll_interval_sec,
                max_work_items_per_tick=max_work_items_per_tick,
                worker_pool_size=per_process_worker_pool_size,
                resource_lock_path=shared_resource_lock_path,
                resource_lock_backend=resource_lock_backend,
                resource_lock_redis_url=resource_lock_redis_url,
                resource_lock_ttl_sec=resource_lock_ttl_sec,
                sandbox_mode=sandbox_mode,
                container_runtime=container_runtime,
                idle_ticks_to_stop=idle_ticks_to_stop,
                stale_heartbeat_after_sec=stale_heartbeat_after_sec,
                ab_round_dir=ab_round_dir,
                ab_round_id=ab_round_id,
                environment=environment,
            )
        )
    return DaemonWorkerPoolServicePlan(
        platform=platform,
        fleet_id=fleet_id,
        replica_count=replica_count,
        per_process_worker_pool_size=per_process_worker_pool_size,
        shared_store_path=str(shared_store_path),
        shared_resource_lock_path=str(shared_resource_lock_path),
        resource_lock_backend=resource_lock_backend,
        plans=plans,
        notes=[
            "Each daemon replica is an independent process with its own heartbeat state.",
            "All replicas share the same Control Plane store and SQLite/file resource lock.",
            "Resource locks and work-item leases prevent duplicate claims and write races.",
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
    Path(plan.stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(plan.stderr_path).parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.content, encoding="utf-8")
    return path


def materialize_daemon_worker_pool_service_plan(
    plan: DaemonWorkerPoolServicePlan,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Write every service file in a multi-process worker-pool plan."""

    return [
        materialize_daemon_service_install_plan(replica_plan, overwrite=overwrite)
        for replica_plan in plan.plans
    ]


def _resolve_under_workdir(workdir: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return workdir / path


def _replica_path(value: str | Path, *, suffix: str, default_ext: str) -> Path:
    path = Path(value)
    extension = path.suffix or default_ext
    return path.with_name(f"{path.stem}.{suffix}{extension}")


def _launchd_plist(
    *,
    service_name: str,
    command: Sequence[str],
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    environment: Mapping[str, str],
) -> str:
    # launchd is much easier to diagnose when the daemon is executed through a
    # tiny shell boundary: it preserves the working directory contract, captures
    # import/CLI errors in the configured log files, and avoids opaque EX_CONFIG
    # exits from direct Python ProgramArguments on some macOS installations.
    shell_command = (
        "printf '[%s] launchd starting KUN V6 daemon\\n' "
        '"$(date -u +%Y-%m-%dT%H:%M:%SZ)"; '
        f"cd {shlex.quote(str(working_directory))} && exec {_join_command(command)}"
    )
    payload = {
        "Label": service_name,
        "ProgramArguments": ["/bin/sh", "-c", shell_command],
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
