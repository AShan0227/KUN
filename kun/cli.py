"""KUN CLI (typer)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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

_KUN_LOCAL_ROOT_ENV = "KUN_LOCAL_STATE_ROOT"


def _resolve_cli_local_state_path(path: Path, *, cwd: Path | None = None) -> Path:
    """Resolve `.kun-local/...` paths even when the current repo is read-only."""

    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    if not expanded.parts or expanded.parts[0] != ".kun-local":
        return expanded.resolve()
    cwd_path = (cwd or Path.cwd()).expanduser().resolve()
    configured_root = os.getenv(_KUN_LOCAL_ROOT_ENV)
    if configured_root:
        base_root = Path(configured_root).expanduser().resolve()
    elif os.access(cwd_path, os.W_OK | os.X_OK):
        base_root = cwd_path / ".kun-local"
    else:
        workspace_hash = hashlib.sha256(str(cwd_path).encode("utf-8")).hexdigest()[:12]
        base_root = (
            Path(tempfile.gettempdir()) / "kun-local-state" / f"{cwd_path.name}-{workspace_hash}"
        )
    relative_parts = expanded.parts[1:]
    if not relative_parts:
        return base_root
    return (base_root / Path(*relative_parts)).resolve()


def _summarize_rainflow_stage_audit(
    workspace_path: object,
    output_dir: object | None = None,
) -> dict[str, object]:
    audit_path = _resolve_rainflow_stage_audit_path(
        workspace_path=workspace_path,
        output_dir=output_dir,
    )
    if audit_path is None or not audit_path.exists():
        return {}
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rainflow_stage_audit_path": str(audit_path)}

    providers = payload.get("providers", [])
    missing_providers = [
        str(provider.get("provider"))
        for provider in providers
        if isinstance(provider, dict) and not provider.get("credentials_ready", False)
    ]
    stages = payload.get("stages", [])
    ready_stages = [
        str(stage.get("stage_id"))
        for stage in stages
        if isinstance(stage, dict) and stage.get("status") == "ready"
    ]
    provider_blocked = [
        stage
        for stage in stages
        if isinstance(stage, dict) and stage.get("status") == "planning_ready_provider_blocked"
    ]
    provider_blocked_stage_ids = [
        str(stage.get("stage_id"))
        for stage in provider_blocked
        if isinstance(stage, dict) and stage.get("stage_id")
    ]
    phase1_acceptance = payload.get("phase1_acceptance", {})
    phase1_ready = bool(
        isinstance(phase1_acceptance, dict) and phase1_acceptance.get("ready", False)
    )
    audit_phase1_evidence_path = (
        phase1_acceptance.get("path") if isinstance(phase1_acceptance, dict) else None
    )
    transition_package_path = _resolve_rainflow_stage1_transition_package_path(
        workspace_path=workspace_path,
        output_dir=output_dir,
    )
    transition_package = _load_rainflow_stage1_transition_package(
        workspace_path=workspace_path,
        output_dir=output_dir,
    )
    transition_evidence = (
        transition_package.get("transition_evidence", {})
        if isinstance(transition_package, dict)
        else {}
    )
    transition_phase1_acceptance = (
        transition_package.get("phase1_acceptance", {})
        if isinstance(transition_package, dict)
        else {}
    )
    effective_phase1_evidence_path, effective_phase1_evidence_source = (
        _rainflow_effective_phase1_evidence_path(
            audit_phase1_evidence_path=audit_phase1_evidence_path,
            transition_phase1_acceptance=transition_phase1_acceptance,
        )
    )
    phase1_evidence_consistency = _rainflow_phase1_evidence_consistency(
        audit_phase1_evidence_path=audit_phase1_evidence_path,
        effective_phase1_evidence_path=effective_phase1_evidence_path,
    )
    transition_evidence_reason = (
        str(transition_evidence.get("reason")).strip()
        if isinstance(transition_evidence, dict) and transition_evidence.get("reason") is not None
        else None
    ) or None
    transition_evidence_message = (
        str(transition_evidence.get("message")).strip()
        if isinstance(transition_evidence, dict) and transition_evidence.get("message") is not None
        else None
    ) or None
    audit_status = str(payload.get("status") or "").strip() or None
    effective_status = None
    if phase1_ready and provider_blocked:
        effective_status = "waiting_external_provider_credentials"
    elif audit_status == "ready":
        effective_status = "ready_for_stage1"
    elif not phase1_ready and audit_status == "blocked":
        effective_status = "repairing_phase1"
    next_blocker = None
    if transition_evidence_reason == "stage1_transition_timeline_missing":
        next_blocker = transition_evidence_reason
    elif provider_blocked:
        next_blocker = provider_blocked[0].get("provider_blocker")
    elif isinstance(phase1_acceptance, dict) and phase1_acceptance.get("reason"):
        next_blocker = phase1_acceptance.get("reason")
    next_action = payload.get("next_action")
    if transition_evidence_message:
        next_action = transition_evidence_message
    return {
        "rainflow_stage_audit_path": str(audit_path),
        "rainflow_stage_audit_status": audit_status,
        "rainflow_phase1_ready": phase1_ready,
        "rainflow_phase1_evidence_path": audit_phase1_evidence_path,
        "rainflow_phase1_evidence_reason": phase1_acceptance.get("reason")
        if isinstance(phase1_acceptance, dict)
        else None,
        "rainflow_phase1_evidence_auto_discovered": phase1_acceptance.get("auto_discovered")
        if isinstance(phase1_acceptance, dict)
        else None,
        "rainflow_phase1_effective_evidence_path": effective_phase1_evidence_path,
        "rainflow_phase1_effective_evidence_source": effective_phase1_evidence_source,
        "rainflow_phase1_evidence_consistency": phase1_evidence_consistency,
        "rainflow_ai_stage_status": provider_blocked[0].get("status")
        if provider_blocked
        else payload.get("status"),
        "rainflow_ready_provider_count": payload.get("ready_provider_count"),
        "rainflow_missing_providers": missing_providers,
        "rainflow_ready_stages": ready_stages,
        "rainflow_provider_blocked_stages": provider_blocked_stage_ids,
        "rainflow_stage1_transition_package_path": str(transition_package_path)
        if transition_package_path is not None
        else None,
        "rainflow_stage1_transition_status": transition_package.get("status")
        if isinstance(transition_package, dict)
        else None,
        "rainflow_stage1_transition_evidence_reason": transition_evidence_reason,
        "rainflow_next_blocker": next_blocker,
        "rainflow_next_action": next_action,
        "effective_mission_status": effective_status,
    }


def _write_rainflow_mission_package_manifest(
    *,
    root_dir: Path,
    mission_id: str,
    workspace_path: object,
    output_dir: object,
    sandbox_root: object,
    ports: object,
) -> Path | None:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (mission_id, workspace_path, output_dir, sandbox_root)
    ):
        return None
    normalized_root = root_dir.expanduser().resolve()
    mission_root = (
        normalized_root if normalized_root.name == mission_id else normalized_root / mission_id
    )
    mission_root.mkdir(parents=True, exist_ok=True)
    manifest_path = mission_root / "mission-package.json"
    manifest_path.write_text(
        json.dumps(
            {
                "mission_id": mission_id,
                "ports": ports if isinstance(ports, dict) else {},
                "workspace_path": str(workspace_path),
                "output_dir": str(output_dir),
                "sandbox_root": str(sandbox_root),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _rainflow_effective_phase1_evidence_path(
    *,
    audit_phase1_evidence_path: object,
    transition_phase1_acceptance: object,
) -> tuple[object, str | None]:
    if not isinstance(transition_phase1_acceptance, dict):
        return audit_phase1_evidence_path, "stage_audit"
    history = transition_phase1_acceptance.get("history", {})
    latest_accepted_path = (
        history.get("latest_accepted_path") if isinstance(history, dict) else None
    )
    transition_path = transition_phase1_acceptance.get("path")
    if isinstance(latest_accepted_path, str) and latest_accepted_path.strip():
        return latest_accepted_path, "stage1_transition_history"
    if isinstance(transition_path, str) and transition_path.strip():
        return transition_path, "stage1_transition_package"
    return audit_phase1_evidence_path, "stage_audit"


def _rainflow_phase1_evidence_consistency(
    *,
    audit_phase1_evidence_path: object,
    effective_phase1_evidence_path: object,
) -> str | None:
    if not isinstance(audit_phase1_evidence_path, str) or not audit_phase1_evidence_path.strip():
        return None
    if (
        not isinstance(effective_phase1_evidence_path, str)
        or not effective_phase1_evidence_path.strip()
    ):
        return "audit_only"
    if audit_phase1_evidence_path == effective_phase1_evidence_path:
        return "aligned"
    return "stale_stage_audit_pointer"


def _resolve_rainflow_stage_audit_path(
    *,
    workspace_path: object,
    output_dir: object | None = None,
) -> Path | None:
    candidates: list[Path] = []
    if isinstance(output_dir, str) and output_dir:
        candidates.append(Path(output_dir).expanduser().resolve())
    if isinstance(workspace_path, str) and workspace_path:
        candidates.append(Path(workspace_path).expanduser().resolve() / "outputs")
    seen: set[Path] = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        audit_path = base / "reports" / "ai-video-stage-gate-audit-latest.json"
        if audit_path.exists():
            return audit_path
    if candidates:
        return candidates[0] / "reports" / "ai-video-stage-gate-audit-latest.json"
    return None


def _load_rainflow_stage1_transition_package(
    *,
    workspace_path: object,
    output_dir: object | None = None,
) -> dict[str, object]:
    manifest_path = _resolve_rainflow_stage1_transition_package_path(
        workspace_path=workspace_path,
        output_dir=output_dir,
    )
    if manifest_path is None or not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"manifest_path": str(manifest_path)}
    if isinstance(payload, dict):
        payload.setdefault("manifest_path", str(manifest_path))
        return payload
    return {"manifest_path": str(manifest_path)}


def _resolve_rainflow_stage1_transition_package_path(
    *,
    workspace_path: object,
    output_dir: object | None = None,
) -> Path | None:
    manifests: list[Path] = []
    for base in _rainflow_stage1_transition_search_roots(
        workspace_path=workspace_path,
        output_dir=output_dir,
    ):
        latest = base / "ai-video-stage1-transition-package" / "manifest.json"
        if latest.exists():
            manifests.append(latest)
        manifests.extend(base.glob("*/ai-video-stage1-transition-package/manifest.json"))
    if not manifests:
        return None
    return max(manifests, key=_rainflow_stage1_transition_manifest_priority)


def _rainflow_stage1_transition_search_roots(
    *,
    workspace_path: object,
    output_dir: object | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    if isinstance(output_dir, str) and output_dir:
        candidates.append(Path(output_dir).expanduser().resolve())
    if isinstance(workspace_path, str) and workspace_path:
        workspace = Path(workspace_path).expanduser().resolve()
        candidates.extend(
            [
                workspace / "outputs",
                workspace / "adflow_extreme_outputs",
                workspace / "adflow-extreme-outputs",
                workspace / "k_output",
                workspace / "k-output",
                workspace / "kun_output",
                workspace / "kun-output",
            ]
        )
    seen: set[Path] = set()
    roots: list[Path] = []
    for base in candidates:
        if base in seen or not base.exists():
            continue
        seen.add(base)
        roots.append(base)
    return roots


def _rainflow_stage1_transition_manifest_priority(path: Path) -> tuple[int, float]:
    status_rank = 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        status_rank = {
            "ready": 3,
            "mock_verified": 2,
            "planning_ready_provider_blocked": 1,
            "blocked": 0,
        }.get(str(payload.get("status") or "").strip(), 0)
    return (status_rank, path.stat().st_mtime)


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
    stale_heartbeat_after_sec: float = typer.Option(
        900.0,
        "--stale-heartbeat-after-sec",
        min=1.0,
        help="超过该秒数没有心跳时标记为 stale/unhealthy",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """查看 KUN V6 Control Plane 后台服务心跳和停止请求。"""

    from kun.control_plane import FileDaemonServiceStateStore, daemon_service_process_is_alive

    state_path = _resolve_cli_local_state_path(state_path)
    state_store = FileDaemonServiceStateStore(state_path)
    state = state_store.load()
    stop_request = state_store.load_stop_request()
    stale = (
        state.is_stale(
            now=datetime.now(UTC),
            stale_after=timedelta(seconds=stale_heartbeat_after_sec),
        )
        if state is not None
        else False
    )
    process_alive = (
        daemon_service_process_is_alive(state.process_id)
        if state is not None and state.status not in {"stopped", "unhealthy"}
        else None
    )
    dead_process = process_alive is False and not stale
    healthy = (
        state is not None
        and state.status not in {"stopped", "unhealthy"}
        and not stale
        and not dead_process
    )
    status = (
        "unhealthy" if stale or dead_process else state.status if state is not None else "stopped"
    )
    payload = {
        "state_path": str(state_path),
        "status": status,
        "healthy": healthy,
        "stale": stale,
        "process_alive": process_alive,
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
    table.add_row("健康", "yes" if healthy else "no")
    table.add_row("心跳过期", "yes" if stale else "no")
    table.add_row("进程存在", "-" if process_alive is None else "yes" if process_alive else "no")
    table.add_row("状态文件", str(state_path))
    table.add_row("最近心跳", str(state.last_heartbeat_at if state is not None else "-"))
    table.add_row(
        "下一次醒来",
        str(state.next_wakeup_at if state is not None and state.next_wakeup_at else "-"),
    )
    table.add_row("停止请求", stop_request.reason if stop_request is not None else "-")
    console.print(table)


@control_plane_app.command("feature-activation-audit")
def control_plane_feature_activation_audit(
    output_dir: Path = typer.Option(
        Path(".kun-local/feature-activation-audit"),
        "--output-dir",
        help="功能激活审计输出目录",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """运行 KUN V6 功能激活任务审计。"""

    from kun.control_plane.feature_activation_audit import run_feature_activation_audit

    output_dir = _resolve_cli_local_state_path(output_dir)
    if json_output:
        with contextlib.redirect_stdout(sys.stderr):
            report = run_feature_activation_audit(output_dir=output_dir)
    else:
        report = run_feature_activation_audit(output_dir=output_dir)
    payload = report.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
        return
    table = Table(title="KUN V6 Feature Activation Audit")
    table.add_column("功能")
    table.add_column("子系统")
    table.add_column("状态")
    table.add_column("触发条件")
    for case in report.cases:
        table.add_row(
            case.feature_id,
            case.subsystem,
            "[green]已激活[/]" if case.activated else "[red]未激活[/]",
            case.trigger_condition,
        )
    console.print(table)
    console.print(f"[green]JSON[/] {report.report_json_path}")
    console.print(f"[green]报告[/] {report.report_markdown_path}")


@control_plane_app.command("rainflow-ad-mission")
def control_plane_rainflow_ad_mission(
    root_dir: Path = typer.Option(
        Path(".kun-local/rainflow-missions"),
        "--root-dir",
        help="RainFlow 隔离 mission 根目录",
    ),
    store_path: Path = typer.Option(
        Path(".kun-local/v6-control-plane.json"),
        "--store-path",
        help="Control Plane 持久 store 路径",
    ),
    mission_id: str = typer.Option(
        "msn-rainflow-ad-video",
        "--mission-id",
        help="RainFlow mission id",
    ),
    owner: str = typer.Option("kun", "--owner", help="mission owner"),
    preferred_app_port: int = typer.Option(3401, "--preferred-app-port", min=1, max=65535),
    preferred_preview_port: int = typer.Option(
        3402,
        "--preferred-preview-port",
        min=1,
        max=65535,
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """创建或继续 RainFlow 信息流广告隔离 mission。"""

    from kun.control_plane import (
        FileControlPlaneStore,
        InMemoryControlPlane,
        build_rainflow_ad_mission,
        build_rainflow_ad_mission_from_record,
        discover_rainflow_mission_packages,
    )

    root_dir = _resolve_cli_local_state_path(root_dir)
    store_path = _resolve_cli_local_state_path(store_path)
    requested_mission_id = mission_id
    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    packages = discover_rainflow_mission_packages(
        root_dir=root_dir,
        recovery_store_paths=_candidate_rainflow_recovery_store_paths(
            store_path=store_path,
            root_dir=root_dir,
        ),
    )
    discovered_package = None
    existing = control_plane.missions.get(mission_id)
    recovered_adoption_reason = None
    discovered_package = _select_rainflow_discovered_package(
        packages=packages,
        requested_mission_id=mission_id,
    )
    if existing is not None and discovered_package is not None:
        if _should_adopt_recovered_rainflow_package(
            control_plane=control_plane,
            existing_mission=existing,
            discovered_package=discovered_package,
        ):
            recovered_adoption_reason = (
                "requested_mission_scaffold_was_blank_and_recovered_workspace_had_more_real_files"
            )
            mission_id = discovered_package.mission_id
            existing = control_plane.missions.get(mission_id)
    elif existing is None and discovered_package is not None:
        recovered_adoption_reason = "recovered_existing_isolated_mission"
        mission_id = discovered_package.mission_id
        existing = control_plane.missions.get(mission_id)
    created = existing is None

    if created:
        if discovered_package is not None:
            package = build_rainflow_ad_mission_from_record(
                discovered_package,
                owner=owner,
            )
        else:
            package = build_rainflow_ad_mission(
                root_dir=root_dir,
                mission_id=mission_id,
                owner=owner,
                preferred_ports=(preferred_app_port, preferred_preview_port),
            )
        mission = control_plane.submit_mission(
            mission=package.mission,
            task_plan=package.task_plan,
            execution_contract=package.execution_contract,
            working_context=package.working_context,
            work_items=package.work_items,
            actor="kun-cli-rainflow-mission",
        )
        contract = package.execution_contract
        work_item_summary = {
            "work_item_ids": [item.work_item_id for item in package.work_items],
            "active_work_item_ids": [item.work_item_id for item in package.work_items],
            "active_work_item_count": len(package.work_items),
            "runnable_work_item_ids": [item.work_item_id for item in package.work_items],
            "runnable_work_item_count": len(package.work_items),
            "dependency_blocked_work_item_ids": [],
            "dependency_blocked_work_item_count": 0,
            "historical_work_item_count": len(package.work_items),
            "completed_work_item_count": 0,
            "cancelled_work_item_count": 0,
            "failed_work_item_count": 0,
            "current_plan_failed_work_item_ids": [],
            "current_plan_failed_work_item_count": 0,
        }
    else:
        mission = existing
        contract = control_plane.contracts.get(mission.execution_contract_ref or "")
        if discovered_package is not None:
            contract = _hydrate_existing_rainflow_isolation_metadata(
                control_plane=control_plane,
                mission=mission,
                execution_contract=contract,
                discovered_package=discovered_package,
            )
        work_item_summary = _summarize_rainflow_mission_work_items(
            control_plane,
            mission_id=mission_id,
            task_plan_version=mission.current_plan_version,
        )

    delivery_contract = contract.delivery_contract if contract is not None else {}
    payload = {
        "created": created,
        "requested_mission_id": requested_mission_id,
        "mission_id": mission.mission_id,
        "mission_status": mission.status,
        "task_plan_version": mission.current_plan_version,
        "workspace_path": delivery_contract.get("workspace_path"),
        "output_dir": delivery_contract.get("output_dir"),
        "sandbox_root": delivery_contract.get("sandbox_root"),
        "workspace_ref": delivery_contract.get("workspace_ref"),
        "sandbox_ref": delivery_contract.get("sandbox_ref"),
        "resource_locks": delivery_contract.get("resource_locks", []),
        "ports": delivery_contract.get("ports", {}),
        "store_path": str(store_path),
        "root_dir": str(root_dir),
    }
    payload.update(work_item_summary)
    payload["recovered_mission_adopted"] = requested_mission_id != mission.mission_id
    payload["recovered_mission_adoption_reason"] = (
        recovered_adoption_reason if payload["recovered_mission_adopted"] else None
    )
    payload["recovered_source_workspace_path"] = (
        discovered_package.workspace_path
        if payload["recovered_mission_adopted"] and discovered_package is not None
        else None
    )
    payload.update(
        _summarize_rainflow_stage_audit(
            payload["workspace_path"],
            payload["output_dir"],
        )
    )
    _apply_rainflow_work_item_blockers(payload)
    _write_rainflow_mission_package_manifest(
        root_dir=root_dir,
        mission_id=mission.mission_id,
        workspace_path=payload["workspace_path"],
        output_dir=payload["output_dir"],
        sandbox_root=payload["sandbox_root"],
        ports=payload["ports"],
    )
    payload.setdefault("effective_mission_status", mission.status)
    if json_output:
        console.print_json(data=payload)
        return
    table = Table(title="RainFlow ad mission")
    table.add_column("字段")
    table.add_column("值")
    table.add_row("created", "yes" if created else "no")
    table.add_row("requested_mission_id", requested_mission_id)
    table.add_row("mission_id", mission.mission_id)
    table.add_row("status", mission.status)
    table.add_row("effective_status", str(payload["effective_mission_status"]))
    table.add_row("plan", str(mission.current_plan_version))
    table.add_row("workspace", str(payload["workspace_path"]))
    table.add_row("outputs", str(payload["output_dir"]))
    table.add_row("ports", json.dumps(payload["ports"], ensure_ascii=False))
    if payload.get("recovered_mission_adopted"):
        table.add_row("recovered_mission_adopted", "yes")
        table.add_row(
            "recovered_mission_adoption_reason",
            str(payload["recovered_mission_adoption_reason"]),
        )
        table.add_row(
            "recovered_source_workspace_path",
            str(payload["recovered_source_workspace_path"]),
        )
    if payload.get("rainflow_stage_audit_status") is not None:
        table.add_row("stage_audit_status", str(payload["rainflow_stage_audit_status"]))
    if payload.get("rainflow_ai_stage_status") is not None:
        table.add_row("ai_stage_status", str(payload["rainflow_ai_stage_status"]))
    if payload.get("rainflow_next_blocker") is not None:
        table.add_row("next_blocker", str(payload["rainflow_next_blocker"]))
    if payload.get("rainflow_next_action") is not None:
        table.add_row("next_action", str(payload["rainflow_next_action"]))
    console.print(table)


def _candidate_rainflow_recovery_store_paths(*, store_path: Path, root_dir: Path) -> list[Path]:
    search_roots = {store_path.parent.resolve(), root_dir.parent.resolve()}
    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in [
        store_path.resolve(),
        *[path for root in search_roots for path in root.glob("*control-plane*.json")],
    ]:
        normalized = candidate.expanduser().resolve()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def _summarize_rainflow_mission_work_items(
    control_plane,
    *,
    mission_id: str,
    task_plan_version: str | None = None,
) -> dict[str, object]:
    mission_work_items = [
        item
        for item in sorted(
            control_plane.work_items.values(),
            key=lambda item: (-item.priority, item.work_item_id),
        )
        if item.mission_id == mission_id
    ]
    active_work_items = [
        item for item in mission_work_items if item.status not in {"done", "cancelled", "failed"}
    ]
    done_work_item_ids = {item.work_item_id for item in mission_work_items if item.status == "done"}
    runnable_work_items = [
        item for item in active_work_items if set(item.dependencies).issubset(done_work_item_ids)
    ]
    dependency_blocked_work_items = [
        item for item in active_work_items if item not in runnable_work_items
    ]
    current_plan_failed_work_items = [
        item
        for item in mission_work_items
        if item.status == "failed"
        and (task_plan_version is None or item.task_plan_version == task_plan_version)
    ]
    return {
        "work_item_ids": [item.work_item_id for item in active_work_items],
        "active_work_item_ids": [item.work_item_id for item in active_work_items],
        "active_work_item_count": len(active_work_items),
        "runnable_work_item_ids": [item.work_item_id for item in runnable_work_items],
        "runnable_work_item_count": len(runnable_work_items),
        "dependency_blocked_work_item_ids": [
            item.work_item_id for item in dependency_blocked_work_items
        ],
        "dependency_blocked_work_item_count": len(dependency_blocked_work_items),
        "historical_work_item_count": len(mission_work_items),
        "completed_work_item_count": sum(1 for item in mission_work_items if item.status == "done"),
        "cancelled_work_item_count": sum(
            1 for item in mission_work_items if item.status == "cancelled"
        ),
        "failed_work_item_count": sum(1 for item in mission_work_items if item.status == "failed"),
        "current_plan_failed_work_item_ids": [
            item.work_item_id for item in current_plan_failed_work_items
        ],
        "current_plan_failed_work_item_count": len(current_plan_failed_work_items),
    }


def _apply_rainflow_work_item_blockers(payload: dict[str, object]) -> None:
    current_plan_failed = payload.get("current_plan_failed_work_item_ids")
    if (
        isinstance(current_plan_failed, list)
        and current_plan_failed
        and not _rainflow_failed_plan_is_superseded(payload)
    ):
        payload["effective_mission_status"] = "repairing_failed_current_plan_work"
        payload["rainflow_next_blocker"] = "current_plan_failed_work"
        payload["rainflow_next_action"] = (
            "Current-plan RainFlow work has already failed locally; repair and retest that path "
            "before treating provider credentials as the next blocker."
        )
        return

    active_count = payload.get("active_work_item_count")
    runnable_count = payload.get("runnable_work_item_count")
    dependency_blocked_count = payload.get("dependency_blocked_work_item_count")
    if (
        isinstance(active_count, int)
        and active_count > 0
        and runnable_count == 0
        and isinstance(dependency_blocked_count, int)
        and dependency_blocked_count > 0
    ):
        payload["effective_mission_status"] = "waiting_on_dependency_chain"


def _rainflow_failed_plan_is_superseded(payload: dict[str, object]) -> bool:
    if payload.get("rainflow_phase1_ready") is not True:
        return False
    if payload.get("rainflow_phase1_evidence_consistency") != "stale_stage_audit_pointer":
        return False
    dependency_blocked_count = payload.get("dependency_blocked_work_item_count")
    if isinstance(dependency_blocked_count, int) and dependency_blocked_count > 0:
        return False
    transition_status = payload.get("rainflow_stage1_transition_status")
    return transition_status in {"mock_verified", "ready"}


def _select_rainflow_discovered_package(
    *,
    packages: Sequence[object],
    requested_mission_id: str,
):
    if not packages:
        return None
    exact = next(
        (package for package in packages if package.mission_id == requested_mission_id), None
    )
    if exact is None:
        return packages[0] if len(packages) == 1 else None
    if len(packages) == 1:
        return exact
    if requested_mission_id == "msn-rainflow-ad-video":
        recovered_packages = [
            package for package in packages if package.mission_id != requested_mission_id
        ]
        if recovered_packages:
            return max(
                recovered_packages,
                key=lambda package: (
                    _rainflow_workspace_signal(Path(package.workspace_path))[
                        "meaningful_file_count"
                    ],
                    _rainflow_workspace_signal(Path(package.workspace_path))["total_file_count"],
                    package.mission_id,
                ),
            )

    exact_signal = _rainflow_workspace_signal(Path(exact.workspace_path))
    if exact_signal["meaningful_file_count"] > 0:
        return exact

    richest = max(
        packages,
        key=lambda package: (
            _rainflow_workspace_signal(Path(package.workspace_path))["meaningful_file_count"],
            _rainflow_workspace_signal(Path(package.workspace_path))["total_file_count"],
            package.mission_id,
        ),
    )
    richest_signal = _rainflow_workspace_signal(Path(richest.workspace_path))
    if richest_signal["meaningful_file_count"] > exact_signal["meaningful_file_count"]:
        return richest
    return exact


def _should_adopt_recovered_rainflow_package(
    *,
    control_plane,
    existing_mission,
    discovered_package,
) -> bool:
    if discovered_package.mission_id == existing_mission.mission_id:
        return False
    existing_contract = control_plane.contracts.get(existing_mission.execution_contract_ref or "")
    existing_delivery = existing_contract.delivery_contract if existing_contract is not None else {}
    existing_workspace = _first_non_empty_path(
        existing_delivery.get("workspace_path"),
        existing_delivery.get("project_path"),
    )
    existing_signal = (
        _rainflow_workspace_signal(Path(existing_workspace))
        if existing_workspace
        else {
            "meaningful_file_count": 0,
            "total_file_count": 0,
        }
    )
    discovered_signal = _rainflow_workspace_signal(Path(discovered_package.workspace_path))
    pristine_scaffold = _rainflow_mission_is_pristine_scaffold(
        control_plane,
        mission_id=existing_mission.mission_id,
    )
    if pristine_scaffold:
        if discovered_signal["meaningful_file_count"] <= 0:
            return False
    elif discovered_signal["meaningful_file_count"] <= existing_signal["meaningful_file_count"]:
        return False
    if existing_signal["meaningful_file_count"] > 0 and not pristine_scaffold:
        return False
    if existing_mission.status not in {"contracted", "queued"}:
        return False
    if any(
        artifact.mission_id == existing_mission.mission_id
        for artifact in control_plane.artifacts.values()
    ):
        return False
    if any(
        manifest.mission_id == existing_mission.mission_id
        for manifest in control_plane.artifact_manifests.values()
    ):
        return False
    if any(
        gate.mission_id == existing_mission.mission_id
        for gate in control_plane.gate_evaluations.values()
    ):
        return False
    if any(
        ticket.mission_id == existing_mission.mission_id
        for ticket in control_plane.collaboration_tickets.values()
    ):
        return False
    if any(
        review.mission_id == existing_mission.mission_id
        for review in control_plane.acceptance_reviews.values()
    ):
        return False
    return pristine_scaffold


def _rainflow_mission_is_pristine_scaffold(control_plane, *, mission_id: str) -> bool:
    return not any(
        item.mission_id == mission_id and item.status != "queued"
        for item in control_plane.work_items.values()
    )


def _hydrate_existing_rainflow_isolation_metadata(
    *,
    control_plane,
    mission,
    execution_contract,
    discovered_package,
):
    if execution_contract is None:
        return execution_contract

    current_delivery = (
        dict(execution_contract.delivery_contract)
        if isinstance(execution_contract.delivery_contract, dict)
        else {}
    )
    workspace_path = _first_non_empty_path(
        current_delivery.get("workspace_path"),
        current_delivery.get("project_path"),
        discovered_package.workspace_path,
    )
    output_dir = _first_non_empty_path(
        current_delivery.get("output_dir"),
        discovered_package.output_dir,
    )
    sandbox_root = _first_non_empty_path(
        current_delivery.get("sandbox_root"),
        discovered_package.sandbox_root,
    )
    if workspace_path is None or output_dir is None or sandbox_root is None:
        return execution_contract

    workspace_ref = f"workspace://{workspace_path}"
    sandbox_ref = f"sandbox://workspace-snapshot/{mission.mission_id}"
    resource_locks = [
        f"mission:{mission.mission_id}",
        f"workspace:{workspace_path}",
        f"output_dir:{output_dir}",
    ]
    updated_delivery = dict(current_delivery)
    updated_delivery["workspace_path"] = workspace_path
    updated_delivery.setdefault("project_path", workspace_path)
    updated_delivery["output_dir"] = output_dir
    updated_delivery["sandbox_root"] = sandbox_root
    updated_delivery["workspace_ref"] = workspace_ref
    updated_delivery["sandbox_ref"] = sandbox_ref
    updated_delivery["resource_locks"] = resource_locks
    ports = discovered_package.ports or _ports_from_delivery_contract(current_delivery)
    if ports:
        updated_delivery["ports"] = ports

    updated_contract = execution_contract
    if updated_delivery != current_delivery:
        updated_contract = execution_contract.model_copy(
            update={"delivery_contract": updated_delivery}
        )
        control_plane.contracts[updated_contract.contract_id] = updated_contract
        if control_plane.store is not None:
            control_plane.store.put_execution_contract(updated_contract)

    for work_item in list(control_plane.work_items.values()):
        if work_item.mission_id != mission.mission_id:
            continue
        update_payload = {}
        if not work_item.workspace_ref:
            update_payload["workspace_ref"] = workspace_ref
        if not work_item.sandbox_ref:
            update_payload["sandbox_ref"] = sandbox_ref
        if not work_item.resource_locks:
            update_payload["resource_locks"] = list(resource_locks)
        if not update_payload:
            continue
        updated_item = work_item.model_copy(update=update_payload)
        control_plane.work_items[updated_item.work_item_id] = updated_item
        if control_plane.store is not None:
            control_plane.store.put_work_item(updated_item)
    return updated_contract


def _rainflow_workspace_signal(workspace_path: Path) -> dict[str, int]:
    root = workspace_path.expanduser().resolve()
    if not root.exists():
        return {"meaningful_file_count": 0, "total_file_count": 0}

    meaningful = 0
    total = 0
    ignored_parts = {".git", ".venv", ".ruff_cache", "__pycache__", ".mypy_cache", ".pytest_cache"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        total += 1
        if ignored_parts.intersection(path.parts):
            continue
        meaningful += 1
    return {"meaningful_file_count": meaningful, "total_file_count": total}


def _first_non_empty_path(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _ports_from_delivery_contract(delivery_contract: object) -> dict[str, int]:
    if not isinstance(delivery_contract, dict):
        return {}
    ports = delivery_contract.get("ports")
    if isinstance(ports, dict):
        app_port = ports.get("app")
        preview_port = ports.get("preview")
        if isinstance(app_port, int) and isinstance(preview_port, int):
            return {"app": app_port, "preview": preview_port}
    preferred_port = delivery_contract.get("preferred_port")
    if isinstance(preferred_port, int):
        return {"app": preferred_port, "preview": preferred_port + 1}
    return {}


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
    clear: bool = typer.Option(False, "--clear", help="清除已有停止请求，允许同 daemon 重启"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """写入持久停止请求，让后台服务安全收尾。"""

    from kun.control_plane import FileDaemonServiceStateStore

    state_path = _resolve_cli_local_state_path(state_path)
    state_store = FileDaemonServiceStateStore(state_path)
    if clear:
        cleared = state_store.clear_stop_request(daemon_id=daemon_id)
        pending = state_store.load_stop_request()
        payload = {
            "accepted": True,
            "cleared": cleared,
            "mismatch": bool(pending is not None and not cleared),
            "state_path": str(state_path),
            "pending_stop_request": pending.model_dump(mode="json") if pending else None,
        }
        if json_output:
            console.print_json(data=payload)
            return
        if cleared:
            console.print(f"[green]stop request cleared[/] {daemon_id}")
        else:
            console.print(f"[yellow]no matching stop request to clear[/] {daemon_id}")
        return
    request = state_store.request_stop(
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
    worker_pool_size: int = typer.Option(
        1,
        "--worker-pool-size",
        min=1,
        help="本 daemon 可调度的 worker 槽位数；多 daemon 可共享资源锁文件",
    ),
    resource_lock_path: Path | None = typer.Option(
        None,
        "--resource-lock-path",
        help="可选共享资源锁文件；多进程/多机器 worker pool 用它协调资源",
    ),
    resource_lock_backend: str = typer.Option(
        "file",
        "--resource-lock-backend",
        help="资源锁后端：file、sqlite 或 redis；多进程推荐 sqlite，跨机器推荐 redis",
    ),
    resource_lock_redis_url: str | None = typer.Option(None, "--resource-lock-redis-url"),
    resource_lock_ttl_sec: float = typer.Option(
        900.0,
        "--resource-lock-ttl-sec",
        min=1,
        help="资源锁和 work item lease 过期时间",
    ),
    sandbox_mode: str = typer.Option(
        "workspace_snapshot",
        "--sandbox-mode",
        help="执行隔离：workspace_snapshot、container_required 或 external_container",
    ),
    container_runtime: str | None = typer.Option(
        None,
        "--container-runtime",
        help="容器隔离运行时，例如 docker/podman；用于驾驶舱和门禁记录",
    ),
    ab_round_dir: Path | None = typer.Option(
        None,
        "--ab-round-dir",
        envvar="KUN_V6_AB_ROUND_DIR",
        help="可选 Frontier50 回归轮次目录；写入常驻服务命令",
    ),
    ab_round_id: str | None = typer.Option(
        None,
        "--ab-round-id",
        envvar="KUN_V6_AB_ROUND_ID",
        help="可选 AB 回归轮次 ID；写入常驻服务命令",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """生成跨重启后台常驻服务配置，不写文件。"""

    from kun.control_plane import build_daemon_service_install_plan

    working_directory = working_directory.expanduser().resolve()
    store_path = _resolve_cli_local_state_path(store_path)
    state_path = _resolve_cli_local_state_path(state_path)
    if resource_lock_path is not None:
        resource_lock_path = _resolve_cli_local_state_path(resource_lock_path)
    if platform not in {"launchd", "systemd"}:
        raise typer.BadParameter("platform must be launchd or systemd")
    if resource_lock_backend not in {"file", "sqlite", "redis"}:
        raise typer.BadParameter("resource_lock_backend must be file, sqlite, or redis")
    if resource_lock_backend == "redis" and not resource_lock_redis_url:
        raise typer.BadParameter("resource_lock_redis_url is required for redis backend")
    plan = build_daemon_service_install_plan(
        platform=platform,  # type: ignore[arg-type]
        service_name=service_name,
        working_directory=working_directory,
        install_path=install_path,
        store_path=store_path,
        state_path=state_path,
        poll_interval_sec=poll_interval_sec,
        max_work_items_per_tick=max_work_items_per_tick,
        worker_pool_size=worker_pool_size,
        resource_lock_path=resource_lock_path,
        resource_lock_backend=resource_lock_backend,
        resource_lock_redis_url=resource_lock_redis_url,
        resource_lock_ttl_sec=resource_lock_ttl_sec,
        sandbox_mode=sandbox_mode,
        container_runtime=container_runtime,
        ab_round_dir=ab_round_dir,
        ab_round_id=ab_round_id,
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
    worker_pool_size: int = typer.Option(1, "--worker-pool-size", min=1),
    resource_lock_path: Path | None = typer.Option(None, "--resource-lock-path"),
    resource_lock_backend: str = typer.Option("file", "--resource-lock-backend"),
    resource_lock_redis_url: str | None = typer.Option(None, "--resource-lock-redis-url"),
    resource_lock_ttl_sec: float = typer.Option(900.0, "--resource-lock-ttl-sec", min=1),
    sandbox_mode: str = typer.Option("workspace_snapshot", "--sandbox-mode"),
    container_runtime: str | None = typer.Option(None, "--container-runtime"),
    ab_round_dir: Path | None = typer.Option(
        None,
        "--ab-round-dir",
        envvar="KUN_V6_AB_ROUND_DIR",
        help="可选 Frontier50 回归轮次目录；写入常驻服务命令",
    ),
    ab_round_id: str | None = typer.Option(
        None,
        "--ab-round-id",
        envvar="KUN_V6_AB_ROUND_ID",
        help="可选 AB 回归轮次 ID；写入常驻服务命令",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已有服务文件"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """落盘 launchd/systemd 服务文件，但不自动启动服务。"""

    from kun.control_plane import (
        build_daemon_service_install_plan,
        materialize_daemon_service_install_plan,
    )

    working_directory = working_directory.expanduser().resolve()
    store_path = _resolve_cli_local_state_path(store_path)
    state_path = _resolve_cli_local_state_path(state_path)
    if resource_lock_path is not None:
        resource_lock_path = _resolve_cli_local_state_path(resource_lock_path)
    if platform not in {"launchd", "systemd"}:
        raise typer.BadParameter("platform must be launchd or systemd")
    if resource_lock_backend not in {"file", "sqlite", "redis"}:
        raise typer.BadParameter("resource_lock_backend must be file, sqlite, or redis")
    if resource_lock_backend == "redis" and not resource_lock_redis_url:
        raise typer.BadParameter("resource_lock_redis_url is required for redis backend")
    plan = build_daemon_service_install_plan(
        platform=platform,  # type: ignore[arg-type]
        service_name=service_name,
        working_directory=working_directory,
        install_path=install_path,
        store_path=store_path,
        state_path=state_path,
        worker_pool_size=worker_pool_size,
        resource_lock_path=resource_lock_path,
        resource_lock_backend=resource_lock_backend,
        resource_lock_redis_url=resource_lock_redis_url,
        resource_lock_ttl_sec=resource_lock_ttl_sec,
        sandbox_mode=sandbox_mode,
        container_runtime=container_runtime,
        ab_round_dir=ab_round_dir,
        ab_round_id=ab_round_id,
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


@control_plane_app.command("daemon-worker-pool-plan")
def control_plane_daemon_worker_pool_plan(
    platform: str = typer.Option("launchd", "--platform"),
    fleet_id: str = typer.Option("kun-control-plane-worker-pool", "--fleet-id"),
    service_name: str = typer.Option("com.kun.control-plane.v6", "--service-name"),
    replica_count: int = typer.Option(2, "--replica-count", min=1),
    per_process_worker_pool_size: int = typer.Option(
        1,
        "--per-process-worker-pool-size",
        min=1,
        help="每个 daemon 进程内部 worker 槽位数",
    ),
    working_directory: Path = typer.Option(Path("."), "--working-directory"),
    store_path: Path = typer.Option(Path(".kun-local/v6-control-plane.json"), "--store-path"),
    state_path: Path = typer.Option(Path(".kun-local/v6-daemon-service.json"), "--state-path"),
    resource_lock_path: Path | None = typer.Option(None, "--resource-lock-path"),
    resource_lock_backend: str = typer.Option("sqlite", "--resource-lock-backend"),
    resource_lock_redis_url: str | None = typer.Option(None, "--resource-lock-redis-url"),
    sandbox_mode: str = typer.Option("workspace_snapshot", "--sandbox-mode"),
    container_runtime: str | None = typer.Option(None, "--container-runtime"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """生成多进程 worker pool 服务计划，共享同一任务队列和资源锁。"""

    from kun.control_plane import build_daemon_worker_pool_service_install_plans

    working_directory = working_directory.expanduser().resolve()
    store_path = _resolve_cli_local_state_path(store_path)
    state_path = _resolve_cli_local_state_path(state_path)
    if resource_lock_path is not None:
        resource_lock_path = _resolve_cli_local_state_path(resource_lock_path)
    if platform not in {"launchd", "systemd"}:
        raise typer.BadParameter("platform must be launchd or systemd")
    if resource_lock_backend not in {"file", "sqlite", "redis"}:
        raise typer.BadParameter("resource_lock_backend must be file, sqlite, or redis")
    if resource_lock_backend == "redis" and not resource_lock_redis_url:
        raise typer.BadParameter("resource_lock_redis_url is required for redis backend")
    plan = build_daemon_worker_pool_service_install_plans(
        platform=platform,  # type: ignore[arg-type]
        fleet_id=fleet_id,
        service_name=service_name,
        replica_count=replica_count,
        per_process_worker_pool_size=per_process_worker_pool_size,
        working_directory=working_directory,
        store_path=store_path,
        state_path=state_path,
        resource_lock_path=resource_lock_path,
        resource_lock_backend=resource_lock_backend,
        resource_lock_redis_url=resource_lock_redis_url,
        sandbox_mode=sandbox_mode,
        container_runtime=container_runtime,
    )
    if json_output:
        console.print_json(data=plan.model_dump(mode="json"))
        return
    table = Table(title="KUN V6 daemon worker pool")
    table.add_column("daemon")
    table.add_column("state")
    table.add_column("lock")
    for replica in plan.plans:
        table.add_row(replica.service_name, replica.state_path, replica.resource_lock_path)
    console.print(table)
    console.print(f"[green]replicas[/] {plan.replica_count}")
    console.print(f"[green]shared store[/] {plan.shared_store_path}")


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
    worker_pool_size: int = typer.Option(
        1,
        "--worker-pool-size",
        min=1,
        help="本 daemon 可调度的 worker 槽位数；多 daemon 可共享资源锁文件",
    ),
    resource_lock_path: Path | None = typer.Option(
        None,
        "--resource-lock-path",
        help="可选共享资源锁文件；多进程/多机器 worker pool 用它协调资源",
    ),
    resource_lock_backend: str = typer.Option(
        "file",
        "--resource-lock-backend",
        help="资源锁后端：file、sqlite 或 redis；多进程推荐 sqlite，跨机器推荐 redis",
    ),
    resource_lock_redis_url: str | None = typer.Option(None, "--resource-lock-redis-url"),
    resource_lock_ttl_sec: float = typer.Option(
        900.0,
        "--resource-lock-ttl-sec",
        min=1,
        help="资源锁和 work item lease 过期时间",
    ),
    sandbox_mode: str = typer.Option(
        "workspace_snapshot",
        "--sandbox-mode",
        help="执行隔离：workspace_snapshot、container_required 或 external_container",
    ),
    container_runtime: str | None = typer.Option(
        None,
        "--container-runtime",
        help="容器隔离运行时，例如 docker/podman；用于驾驶舱和门禁记录",
    ),
    ab_round_dir: Path | None = typer.Option(
        None,
        "--ab-round-dir",
        envvar="KUN_V6_AB_ROUND_DIR",
        help="可选 Frontier50 回归轮次目录；提供后 daemon 可执行 Qi AB runner work item",
    ),
    ab_round_id: str = typer.Option(
        "round-02-regression",
        "--ab-round-id",
        envvar="KUN_V6_AB_ROUND_ID",
        help="AB 回归轮次 ID",
    ),
    frontier50_live_workdir: Path | None = typer.Option(
        None,
        "--frontier50-live-workdir",
        envvar="KUN_FRONTIER50_LIVE_WORKDIR",
        help="可选 Frontier50 live 工作区；提供后 daemon 可直接执行 AB round work item",
    ),
    frontier50_live_run_tag: str | None = typer.Option(
        None,
        "--frontier50-live-run-tag",
        envvar="KUN_FRONTIER50_LIVE_RUN_TAG",
        help="可选 Frontier50 live RUN_TAG；为空时按 round 自动生成",
    ),
    frontier50_live_command_timeout_sec: int | None = typer.Option(
        None,
        "--frontier50-live-command-timeout-sec",
        envvar="KUN_FRONTIER50_LIVE_COMMAND_TIMEOUT_SEC",
        min=1,
        help="Frontier50 live 外部命令总超时",
    ),
    enable_mission_director: bool = typer.Option(
        True,
        "--enable-mission-director/--disable-mission-director",
        help="启用 Mission Director 常驻监督角色",
    ),
    mission_director_model_id: str = typer.Option(
        "gpt-5.5",
        "--mission-director-model-id",
        envvar="KUN_MISSION_DIRECTOR_MODEL_ID",
        help="Mission Director 使用的独立模型 ID",
    ),
    mission_director_model_tier: str = typer.Option(
        "top",
        "--mission-director-model-tier",
        envvar="KUN_MISSION_DIRECTOR_MODEL_TIER",
        help="Mission Director 模型档位：top、strong、coding、cheap、fallback",
    ),
    mission_director_provider: str = typer.Option(
        "configured",
        "--mission-director-provider",
        envvar="KUN_MISSION_DIRECTOR_PROVIDER",
        help="Mission Director 模型 provider 标识",
    ),
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
    clear_stop_request: bool = typer.Option(
        False,
        "--clear-stop-request",
        help="启动前清除同 daemon_id 的持久停止请求",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """以前台服务方式运行 KUN V6 Control Plane daemon。"""

    from kun.control_plane import (
        ControlPlaneDaemon,
        DaemonServiceConfig,
        FileControlPlaneStore,
        FileDaemonServiceStateStore,
        FileResourceLockStore,
        InMemoryControlPlane,
        RedisResourceLockStore,
        SQLiteResourceLockStore,
        WorkerPoolConfig,
    )
    from kun.control_plane.external_sample_comparison import (
        KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER,
        ExternalSampleComparisonRunner,
    )
    from kun.control_plane.frontier50_external import Frontier50ExternalRuntimeRunner
    from kun.control_plane.game_production import (
        EXTERNAL_SUPERVISOR_GATE_OWNER,
        KUN_GAME_PRODUCTION_RUNNER_OWNER,
        GameProductionRunner,
    )
    from kun.control_plane.kun_runtime_runner import KunRuntimeTaskRunner
    from kun.control_plane.mission_director import (
        MISSION_DIRECTOR_OWNER,
        MissionDirectorModelConfig,
        MissionDirectorRunner,
    )
    from kun.control_plane.productization import ProductizationDogfoodRunner
    from kun.control_plane.runtime_followups import (
        ChainedControlPlaneRunner,
        NuoRuntimeRepairRunner,
        QiRuntimeGovernanceRunner,
    )

    store_path = _resolve_cli_local_state_path(store_path)
    state_path = _resolve_cli_local_state_path(state_path)
    if resource_lock_path is not None:
        resource_lock_path = _resolve_cli_local_state_path(resource_lock_path)
    if max_ticks is not None and max_ticks <= 0:
        raise typer.BadParameter("max_ticks must be positive when provided")
    selected_mission_ids = (
        [mission_id.strip() for mission_id in mission_ids.split(",") if mission_id.strip()]
        if mission_ids
        else None
    )
    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    state_store = FileDaemonServiceStateStore(state_path)
    if clear_stop_request:
        state_store.clear_stop_request(daemon_id=daemon_id)
    if sandbox_mode not in {"workspace_snapshot", "container_required", "external_container"}:
        raise typer.BadParameter(
            "sandbox_mode must be workspace_snapshot, container_required, or external_container"
        )
    if resource_lock_backend not in {"file", "sqlite", "redis"}:
        raise typer.BadParameter("resource_lock_backend must be file, sqlite, or redis")
    if resource_lock_backend == "redis" and not resource_lock_redis_url:
        raise typer.BadParameter("resource_lock_redis_url is required for redis backend")
    if mission_director_model_tier not in {"top", "strong", "coding", "cheap", "fallback"}:
        raise typer.BadParameter(
            "mission_director_model_tier must be top, strong, coding, cheap, or fallback"
        )
    productization_runner = ProductizationDogfoodRunner(
        control_plane=control_plane,
        ab_round_dir=ab_round_dir,
        ab_round_id=ab_round_id,
    )
    external_sample_runner = ExternalSampleComparisonRunner(control_plane=control_plane)
    kun_runner = KunRuntimeTaskRunner(control_plane=control_plane)
    game_production_runner = GameProductionRunner(control_plane=control_plane)
    mission_director_runner = MissionDirectorRunner(
        control_plane=control_plane,
        model_config=MissionDirectorModelConfig(
            model_id=mission_director_model_id,
            model_tier=mission_director_model_tier,
            provider=mission_director_provider,
        ),
    )
    qi_runners = [productization_runner]
    if frontier50_live_workdir is not None:
        qi_runners.append(
            Frontier50ExternalRuntimeRunner(
                workdir=frontier50_live_workdir.expanduser().resolve(),
                run_tag=frontier50_live_run_tag,
                command_timeout_sec=frontier50_live_command_timeout_sec,
            )
        )
    qi_runners.append(QiRuntimeGovernanceRunner(control_plane=control_plane))
    nuo_runners = [
        productization_runner,
        NuoRuntimeRepairRunner(control_plane=control_plane),
    ]
    control_plane_runners = [
        productization_runner,
        NuoRuntimeRepairRunner(control_plane=control_plane),
    ]
    productization_owners = {
        "control-plane": ChainedControlPlaneRunner(
            runner_identity="control-plane-runtime-router",
            runners=control_plane_runners,
        ),
        "control-plane-supervisor": kun_runner,
        "kun": kun_runner,
        KUN_GAME_PRODUCTION_RUNNER_OWNER: game_production_runner,
        EXTERNAL_SUPERVISOR_GATE_OWNER: game_production_runner,
        "qi": ChainedControlPlaneRunner(
            runner_identity="qi-control-plane-runtime-router",
            runners=qi_runners,
        ),
        "nuo": ChainedControlPlaneRunner(
            runner_identity="nuo-control-plane-runtime-router",
            runners=nuo_runners,
        ),
        KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER: external_sample_runner,
    }
    if enable_mission_director:
        productization_owners[MISSION_DIRECTOR_OWNER] = mission_director_runner
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id=daemon_id,
        runners_by_owner=productization_owners,
        worker_pool=WorkerPoolConfig(
            pool_id=f"{daemon_id}-pool",
            machine_id=os.uname().nodename if hasattr(os, "uname") else "local",
            worker_count=worker_pool_size,
        ),
        resource_lock_store=(
            RedisResourceLockStore(resource_lock_redis_url)
            if resource_lock_backend == "redis"
            else SQLiteResourceLockStore(
                resource_lock_path
                if resource_lock_path is not None
                else store_path.with_name(f"{store_path.stem}.resource-locks.sqlite3")
            )
            if resource_lock_backend == "sqlite"
            else FileResourceLockStore(
                resource_lock_path
                if resource_lock_path is not None
                else store_path.with_name(f"{store_path.stem}.resource-locks.json")
            )
        ),
        resource_lock_ttl_sec=resource_lock_ttl_sec,
        sandbox_mode=sandbox_mode,  # type: ignore[arg-type]
        container_runtime=container_runtime,
    )
    config = DaemonServiceConfig(
        poll_interval_sec=poll_interval_sec,
        max_work_items_per_tick=max_work_items_per_tick,
        worker_pool_size=worker_pool_size,
        resource_lock_backend=resource_lock_backend,
        resource_lock_redis_url=resource_lock_redis_url,
        resource_lock_ttl_sec=resource_lock_ttl_sec,
        sandbox_mode=sandbox_mode,
        container_runtime=container_runtime,
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
