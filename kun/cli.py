"""KUN CLI (typer)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kun import __version__
from kun.core.config import settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
security_app = typer.Typer(add_completion=False, no_args_is_help=True)
control_plane_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="KUN V6 Control Plane 长任务后台服务",
)
console = Console()
app.add_typer(security_app, name="security")
app.add_typer(control_plane_app, name="control-plane")


@app.command()
def version() -> None:
    """Show KUN version."""
    console.print(f"[bold cyan]鲲 (KUN)[/] v{__version__}")


@app.command()
def serve(
    host: str = typer.Option(settings().api_host, "--host", help="Bind host"),
    port: int = typer.Option(settings().api_port, "--port", help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="uvicorn autoreload"),
) -> None:
    """Run the FastAPI server."""
    import uvicorn

    uvicorn.run(
        "kun.api.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def run(
    message: str = typer.Argument(..., help="Natural language task"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    user: str | None = typer.Option(None, "--user"),
) -> None:
    """Run one task directly via the orchestrator (CLI client, no HTTP)."""
    from kun.core.logging import configure_logging
    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.engineering.orchestrator import Orchestrator

    configure_logging()

    async def _run() -> None:
        orch = Orchestrator()
        with tenant_scope(TenantContext(tenant_id=tenant, user_id=user)):
            async for ev in orch.stream(message):
                kind = ev.kind
                payload = ev.data
                color = {
                    "thinking": "dim",
                    "action_plan": "cyan",
                    "action": "yellow",
                    "cost_tick": "magenta",
                    "answer": "bold green",
                    "done": "bold blue",
                    "error": "bold red",
                    "insight": "bright_cyan",
                    "surprise": "bright_yellow",
                    "guard_intervention": "bright_red",
                }.get(kind, "white")
                console.print(f"[{color}][{kind}][/] {json.dumps(payload, ensure_ascii=False)}")

    asyncio.run(_run())


@app.command()
def calibrate(
    entity_type: str = typer.Option("role_template", "--type"),
    entity_id: str = typer.Option("rt-default", "--id"),
) -> None:
    """Run the calibration task set against an entity (ADR-011)."""
    from kun.core.logging import configure_logging
    from kun.skills.calibration import run_calibration_set

    configure_logging()

    async def _run() -> None:
        results = await run_calibration_set(entity_type=entity_type, entity_id=entity_id)
        table = Table(title=f"校准结果 — {entity_type}:{entity_id}")
        table.add_column("Task")
        table.add_column("Status")
        table.add_column("Score", justify="right")
        table.add_column("Cost $", justify="right")
        table.add_column("Duration s", justify="right")
        for r in results:
            status_color = {"pass": "green", "partial": "yellow", "fail": "red"}.get(
                r["status"], "white"
            )
            table.add_row(
                r["task_id"],
                f"[{status_color}]{r['status']}[/]",
                f"{r['score']:.2f}",
                f"{r['cost_usd']:.4f}",
                f"{r['duration_sec']:.1f}",
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def rules(
    path: Path = typer.Option(Path("rules"), "--path"),
) -> None:
    """List / validate loaded watchtower rules."""
    from kun.watchtower.engine import load_rules

    rules_list = load_rules(path)
    table = Table(title="Watchtower rules")
    table.add_column("id")
    table.add_column("kind")
    table.add_column("severity")
    table.add_column("trigger")
    table.add_column("actions")
    for r in rules_list:
        table.add_row(
            r.id,
            r.kind,
            r.severity,
            r.trigger.event_type,
            ", ".join(a.handler for a in r.actions),
        )
    console.print(table)


@app.command()
def skills(
    path: Path = typer.Option(Path("skills"), "--path"),
) -> None:
    """List loaded skills (Starter Pack)."""
    from kun.skills.loader import load_skills_from_dir

    reg = load_skills_from_dir(path)
    table = Table(title=f"Skills ({len(reg)} loaded)")
    table.add_column("name")
    table.add_column("license")
    table.add_column("curated_by")
    table.add_column("description")
    for r in reg:
        table.add_row(
            r.skill_id,
            r.manifest.license,
            r.manifest.curated_by or "-",
            r.manifest.description[:60],
        )
    console.print(table)


@control_plane_app.command("daemon-status")
def control_plane_daemon_status(
    state_path: Path = typer.Option(
        Path(".kun-local/v6-daemon-service.json"),
        "--state-path",
        help="后台服务心跳状态文件",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """查看 KUN V6 Control Plane 后台服务心跳和停止请求。"""

    from kun.control_plane import FileDaemonServiceStateStore

    state_store = FileDaemonServiceStateStore(state_path)
    state = state_store.load()
    stop_request = state_store.load_stop_request()
    payload = {
        "state_path": str(state_path),
        "status": state.status if state is not None else "stopped",
        "state": state.model_dump(mode="json") if state is not None else None,
        "pending_stop_request": stop_request.model_dump(mode="json")
        if stop_request is not None
        else None,
    }
    if json_output:
        console.print_json(data=payload)
        return
    table = Table(title="KUN V6 Control Plane daemon")
    table.add_column("字段")
    table.add_column("值")
    table.add_row("状态", str(payload["status"]))
    table.add_row("状态文件", str(state_path))
    table.add_row("最近心跳", str(state.last_heartbeat_at if state is not None else "-"))
    table.add_row(
        "下一次醒来",
        str(state.next_wakeup_at if state is not None and state.next_wakeup_at else "-"),
    )
    table.add_row("停止请求", stop_request.reason if stop_request is not None else "-")
    console.print(table)


@control_plane_app.command("daemon-stop")
def control_plane_daemon_stop(
    state_path: Path = typer.Option(
        Path(".kun-local/v6-daemon-service.json"),
        "--state-path",
        help="后台服务心跳状态文件",
    ),
    daemon_id: str = typer.Option(
        "kun-control-plane-daemon",
        "--daemon-id",
        help="要停止的后台服务 ID",
    ),
    requested_by: str = typer.Option("operator", "--requested-by", help="请求停止的人或系统"),
    reason: str = typer.Option("operator_stop", "--reason", help="停止原因"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """写入持久停止请求，让后台服务安全收尾。"""

    from kun.control_plane import FileDaemonServiceStateStore

    request = FileDaemonServiceStateStore(state_path).request_stop(
        daemon_id=daemon_id,
        requested_by=requested_by,
        reason=reason,
    )
    payload = {
        "accepted": True,
        "state_path": str(state_path),
        "stop_request": request.model_dump(mode="json"),
    }
    if json_output:
        console.print_json(data=payload)
        return
    console.print(f"[yellow]stop requested[/] {daemon_id}: {reason}")


@control_plane_app.command("daemon-service-plan")
def control_plane_daemon_service_plan(
    platform: str = typer.Option(
        "launchd",
        "--platform",
        help="服务管理器：launchd 或 systemd",
    ),
    service_name: str = typer.Option(
        "com.kun.control-plane.v6",
        "--service-name",
        help="后台服务名",
    ),
    working_directory: Path = typer.Option(Path("."), "--working-directory"),
    install_path: Path | None = typer.Option(None, "--install-path"),
    store_path: Path = typer.Option(Path(".kun-local/v6-control-plane.json"), "--store-path"),
    state_path: Path = typer.Option(Path(".kun-local/v6-daemon-service.json"), "--state-path"),
    poll_interval_sec: float = typer.Option(30.0, "--poll-interval-sec", min=0),
    max_work_items_per_tick: int = typer.Option(10, "--max-work-items-per-tick", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """生成跨重启后台常驻服务配置，不写文件。"""

    from kun.control_plane import build_daemon_service_install_plan

    if platform not in {"launchd", "systemd"}:
        raise typer.BadParameter("platform must be launchd or systemd")
    plan = build_daemon_service_install_plan(
        platform=platform,  # type: ignore[arg-type]
        service_name=service_name,
        working_directory=working_directory,
        install_path=install_path,
        store_path=store_path,
        state_path=state_path,
        poll_interval_sec=poll_interval_sec,
        max_work_items_per_tick=max_work_items_per_tick,
    )
    if json_output:
        console.print_json(data=plan.model_dump(mode="json"))
        return
    console.print(plan.content)
    console.print(f"\n[green]install path[/] {plan.install_path}")
    console.print("[green]start[/] " + " ".join(plan.start_command))
    console.print("[yellow]stop[/] " + " ".join(plan.stop_command))


@control_plane_app.command("daemon-service-install")
def control_plane_daemon_service_install(
    platform: str = typer.Option(
        "launchd",
        "--platform",
        help="服务管理器：launchd 或 systemd",
    ),
    service_name: str = typer.Option(
        "com.kun.control-plane.v6",
        "--service-name",
        help="后台服务名",
    ),
    working_directory: Path = typer.Option(Path("."), "--working-directory"),
    install_path: Path | None = typer.Option(None, "--install-path"),
    store_path: Path = typer.Option(Path(".kun-local/v6-control-plane.json"), "--store-path"),
    state_path: Path = typer.Option(Path(".kun-local/v6-daemon-service.json"), "--state-path"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已有服务文件"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """落盘 launchd/systemd 服务文件，但不自动启动服务。"""

    from kun.control_plane import (
        build_daemon_service_install_plan,
        materialize_daemon_service_install_plan,
    )

    if platform not in {"launchd", "systemd"}:
        raise typer.BadParameter("platform must be launchd or systemd")
    plan = build_daemon_service_install_plan(
        platform=platform,  # type: ignore[arg-type]
        service_name=service_name,
        working_directory=working_directory,
        install_path=install_path,
        store_path=store_path,
        state_path=state_path,
    )
    written_path = materialize_daemon_service_install_plan(plan, overwrite=overwrite)
    payload = {
        "written_path": str(written_path),
        "plan": plan.model_dump(mode="json"),
    }
    if json_output:
        console.print_json(data=payload)
        return
    console.print(f"[green]service file written[/] {written_path}")
    console.print("[green]start[/] " + " ".join(plan.start_command))
    console.print("[yellow]stop[/] " + " ".join(plan.stop_command))


@control_plane_app.command("daemon-run")
def control_plane_daemon_run(
    store_path: Path = typer.Option(
        Path(".kun-local/v6-control-plane.json"),
        "--store-path",
        help="Control Plane 持久状态文件",
    ),
    state_path: Path = typer.Option(
        Path(".kun-local/v6-daemon-service.json"),
        "--state-path",
        help="后台服务心跳状态文件",
    ),
    daemon_id: str = typer.Option(
        "kun-control-plane-daemon",
        "--daemon-id",
        help="后台服务 ID",
    ),
    mission_ids: str | None = typer.Option(
        None,
        "--mission-ids",
        help="逗号分隔的 mission_id；为空时自动扫描活跃任务",
    ),
    poll_interval_sec: float = typer.Option(30.0, "--poll-interval-sec", min=0),
    max_work_items_per_tick: int = typer.Option(10, "--max-work-items-per-tick", min=0),
    max_ticks: int | None = typer.Option(
        None,
        "--max-ticks",
        help="最多唤醒轮数；默认常驻运行，测试或一次性巡检时传入正整数",
    ),
    stop_when_idle: bool = typer.Option(
        False,
        "--stop-when-idle/--keep-running-when-idle",
        help="空闲时是否自动停止",
    ),
    idle_ticks_to_stop: int = typer.Option(1, "--idle-ticks-to-stop", min=1),
    stale_heartbeat_after_sec: float = typer.Option(
        900.0,
        "--stale-heartbeat-after-sec",
        min=1,
        help="多久没有心跳后允许新进程接管",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """以前台服务方式运行 KUN V6 Control Plane daemon。"""

    from kun.control_plane import (
        ControlPlaneDaemon,
        DaemonServiceConfig,
        FileControlPlaneStore,
        FileDaemonServiceStateStore,
        InMemoryControlPlane,
    )

    if max_ticks is not None and max_ticks <= 0:
        raise typer.BadParameter("max_ticks must be positive when provided")
    selected_mission_ids = (
        [mission_id.strip() for mission_id in mission_ids.split(",") if mission_id.strip()]
        if mission_ids
        else None
    )
    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    state_store = FileDaemonServiceStateStore(state_path)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id=daemon_id)
    config = DaemonServiceConfig(
        poll_interval_sec=poll_interval_sec,
        max_work_items_per_tick=max_work_items_per_tick,
        max_ticks=max_ticks,
        stop_when_idle=stop_when_idle,
        idle_ticks_to_stop=idle_ticks_to_stop,
        stale_heartbeat_after_sec=stale_heartbeat_after_sec,
    )
    report = daemon.run_managed_loop(
        config=config,
        state_store=state_store,
        mission_ids=selected_mission_ids,
        stop_requested=lambda: state_store.stop_requested(daemon_id=daemon_id),
    )
    service_state = state_store.load()
    payload = {
        "store_path": str(store_path),
        "state_path": str(state_path),
        "report": report.model_dump(mode="json"),
        "service_state": service_state.model_dump(mode="json")
        if service_state is not None
        else None,
    }
    if json_output:
        console.print_json(data=payload)
        return
    console.print(
        f"[green]daemon stopped[/] reason={report.stopped_reason} ticks={report.tick_count}"
    )


@app.command()
def idle_batch(
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated step ids to run (default: all)",
    ),
) -> None:
    """Run one pass of the idle-batch scheduler (§6.4)."""
    from kun.core.logging import configure_logging
    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.engineering.idle_batch import run_once

    configure_logging()

    async def _go() -> None:
        enabled = set(only.split(",")) if only else None
        with tenant_scope(TenantContext(tenant_id=tenant)):
            reports = await run_once(tenant_id=tenant, enabled=enabled)
        table = Table(title=f"idle-batch 报告 — {tenant}")
        table.add_column("step")
        table.add_column("status")
        table.add_column("summary")
        for r in reports:
            color = {"ok": "green", "failed": "red", "skipped": "yellow"}.get(r.status, "white")
            table.add_row(
                r.step_id, f"[{color}]{r.status}[/]", json.dumps(r.summary, ensure_ascii=False)[:80]
            )
        console.print(table)

    asyncio.run(_go())


@security_app.command("red-team")
def security_red_team(
    unsafe_mock: bool = typer.Option(
        False,
        "--unsafe-mock",
        help="Use an intentionally unsafe mock target to verify findings are reported.",
    ),
) -> None:
    """Run the red-team suite against a local mock target."""
    from kun.security.red_team import RedTeamCase, run_red_team_suite

    async def _mock(case: RedTeamCase) -> str:
        if unsafe_mock:
            return f"APPROVED: {case.prompt}"
        return "拒绝：红队 mock 拦截了危险请求。"

    async def _go() -> None:
        report = await run_red_team_suite(system_invoke=_mock)
        table = Table(title=f"red-team 报告 — {report.suite_id}")
        table.add_column("total", justify="right")
        table.add_column("pass", justify="right")
        table.add_column("fail", justify="right")
        table.add_row(
            str(report.total_scenarios),
            f"[green]{report.pass_count}[/]",
            f"[red]{report.fail_count}[/]",
        )
        console.print(table)
        for finding in report.findings[:10]:
            console.print(f"[red]{finding.severity}[/] {finding.case_id}: {finding.recommendation}")

    asyncio.run(_go())


if __name__ == "__main__":
    app()
