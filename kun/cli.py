"""KUN CLI (typer)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.table import Table

from kun import __version__
from kun.core.config import settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
security_app = typer.Typer(add_completion=False, no_args_is_help=True)
promises_app = typer.Typer(add_completion=False, no_args_is_help=True)
release_app = typer.Typer(add_completion=False, no_args_is_help=True)
ops_app = typer.Typer(add_completion=False, no_args_is_help=True, help="生产准入 / 租户启动工具")
protocol_app = typer.Typer(add_completion=False, no_args_is_help=True)
qi_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="启 (Qi) — KUN 子模式: 探索/沉淀协议 (V2.3)"
)
lab_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="KUN-Lab 内测分区 (V2.2 §26)"
)
lab_benchmark_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="KUN-Lab benchmark suites"
)
console = Console()
app.add_typer(security_app, name="security")
app.add_typer(promises_app, name="promises")
app.add_typer(release_app, name="release")
app.add_typer(ops_app, name="ops")
app.add_typer(protocol_app, name="protocol")
app.add_typer(qi_app, name="qi")
app.add_typer(lab_app, name="lab")
lab_app.add_typer(lab_benchmark_app, name="benchmark")


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]


@app.command()
def version() -> None:
    """Show KUN version."""
    console.print(f"[bold cyan]鲲 (KUN)[/] v{__version__}")


@app.command()
def doctor(
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    fix: bool = typer.Option(False, "--fix", help="Try to auto-fix issues found"),
) -> None:
    """KUN 自检 — 交付前/启动前快速 smoke check.

    检查项 (~10 个):
      1. python deps (kun, pydantic, sqlalchemy, prometheus_client)
      2. alembic head (0016_pheromone)
      3. ProtocolRegistry 装上 + 5 seed protocol 在
      4. PheromoneStorage 装上
      5. Prometheus metrics 都注册了
      6. CronScheduler + qi cron jobs 注册
      7. LLM router (claude_code_cli / codex_mcp) 配置好
      8. Frontend 目录存在 + node_modules 装了
      9. 端口 3001 (frontend) 是否被占
      10. /metrics + /api/protocols endpoint 可注册
    """
    import socket
    from pathlib import Path
    from types import SimpleNamespace

    from starlette.datastructures import State

    console.print(f"[bold cyan]KUN doctor[/] — tenant={tenant}\n")
    issues: list[str] = []

    def _ok(msg: str) -> None:
        console.print(f"  [green]✓[/] {msg}")

    def _warn(msg: str) -> None:
        console.print(f"  [yellow]⚠[/] {msg}")
        issues.append(msg)

    def _fail(msg: str) -> None:
        console.print(f"  [red]✗[/] {msg}")
        issues.append(msg)

    # 1. python deps
    try:
        import pydantic  # noqa: F401
        import sqlalchemy  # noqa: F401

        _ok("Python deps: pydantic + sqlalchemy + prometheus_client")
    except ImportError as e:
        _fail(f"Python deps missing: {e}")

    # 2. alembic head
    try:
        import subprocess

        out = subprocess.run(
            ["uv", "run", "alembic", "current"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if "0016_pheromone" in out.stdout:
            _ok("alembic head: 0016_pheromone")
        else:
            _warn(f"alembic head 不在 0016_pheromone (实际: {out.stdout.strip()[:100]})")
    except Exception as e:
        _warn(f"alembic check failed: {e}")

    # 3-5: install_runtime + protocol seed + metrics
    try:
        from kun.api.runtime import install_runtime
        from kun.qi.seed_protocols import seed_default_protocols
        from kun.watchtower.engine import RuleEngine

        app = SimpleNamespace(state=State())
        install_runtime(app, rule_engine=RuleEngine([]))

        if getattr(app.state, "protocol_registry", None) is not None:
            _ok("ProtocolRegistry 装上 (KUN_QI_RUNTIME_ENABLED default ON)")

            async def _seed() -> int:
                return await seed_default_protocols(app.state.protocol_registry)

            n = asyncio.run(_seed())
            listed = asyncio.run(app.state.protocol_registry.list_all(tenant))
            if len(listed) >= 5:
                _ok(f"Protocol seed: {len(listed)} protocols (新 seed {n})")
            else:
                _warn(f"Protocol seed 数量异常: {len(listed)} (期望 ≥ 5)")
        else:
            _fail("ProtocolRegistry 未装 (KUN_QI_RUNTIME_ENABLED=0?)")

        if getattr(app.state, "pheromone_storage", None) is not None:
            _ok("PheromoneStorage 装上")
        else:
            _warn("PheromoneStorage 未装")

        if getattr(app.state, "orchestrator", None) is not None:
            orch = app.state.orchestrator
            hooks = []
            if orch.protocol_registry is not None:
                hooks.append("protocol_registry")
            if orch.anti_gaming_detector is not None:
                hooks.append("anti_gaming")
            if orch.prediction_provider is not None or orch.model_updater is not None:
                hooks.append("predictive_coding")
            _ok(f"Orchestrator V2.3 hooks: {', '.join(hooks) or '(none)'}")
        else:
            _fail("Orchestrator 未装")
    except Exception as e:
        _fail(f"install_runtime failed: {e}")

    # 6. metrics
    try:
        from kun.core.metrics import (
            anti_gaming_detection_total,  # noqa: F401
            protocol_match_total,  # noqa: F401
            qi_window_active,  # noqa: F401
        )

        _ok("Prometheus metrics 已注册 (qi_window_active / protocol_match / anti_gaming)")
    except ImportError as e:
        _fail(f"Metrics 未注册: {e}")

    # 7. LLM router
    try:
        from kun.interface.llm import get_router

        router = get_router()
        providers = getattr(router, "providers", {}) or {}
        if providers:
            tiers = list(providers.keys())
            _ok(f"LLM router: {len(providers)} providers (tiers: {tiers})")
        else:
            _warn("LLM router 0 providers (CLI OAuth 未登录? 检查 claude / codex)")
    except Exception as e:
        _warn(f"LLM router check failed: {e}")

    # 8. Frontend
    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.is_dir():
        if (frontend_dir / "node_modules").is_dir():
            _ok(f"Frontend 装好: {frontend_dir} (node_modules ✓)")
        else:
            _warn(f"Frontend node_modules 缺: cd {frontend_dir} && npm install")
    else:
        _warn(f"Frontend 目录不存在: {frontend_dir}")

    # 9. Frontend port (3001)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 3001))
        sock.close()
        _ok("Port 3001 (frontend) free")
    except OSError:
        sock.close()
        _warn("Port 3001 已被占 — frontend 启动会失败 (用 PORT=3002 npm run dev 换端口)")

    # 10. Backend port (8000)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 8000))
        sock.close()
        _ok("Port 8000 (backend) free")
    except OSError:
        sock.close()
        _warn("Port 8000 已被占 — backend 启动会失败 (用 kun serve --port 8001)")

    # 总结
    console.print()
    if not issues:
        console.print("[bold green]✓ KUN doctor: 全部检查通过, ready to go.[/]")
    else:
        console.print(f"[bold yellow]⚠ KUN doctor: {len(issues)} 个问题需注意.[/]")
        if fix:
            console.print("[cyan]--fix 模式: 尝试自动修复...[/]")
            # Auto-fix: alembic upgrade head (最常见问题)
            for issue in issues:
                if "alembic head" in issue:
                    console.print("  [yellow]→[/] 跑 alembic upgrade head")
                    try:
                        out = subprocess.run(
                            ["uv", "run", "alembic", "upgrade", "head"],  # noqa: S607
                            capture_output=True,
                            text=True,
                            timeout=60,
                            check=False,
                        )
                        if out.returncode == 0:
                            console.print("    [green]✓ alembic upgraded[/]")
                        else:
                            console.print(f"    [red]✗ failed: {out.stderr[:200]}[/]")
                    except Exception as e:
                        console.print(f"    [red]✗ {e}[/]")
                elif "node_modules" in issue:
                    console.print("  [yellow]→[/] 跑 npm install in frontend/")
                    try:
                        frontend_dir = Path(__file__).parent.parent / "frontend"
                        out = subprocess.run(
                            ["npm", "install"],  # noqa: S607
                            cwd=str(frontend_dir),
                            capture_output=True,
                            text=True,
                            timeout=300,
                            check=False,
                        )
                        if out.returncode == 0:
                            console.print("    [green]✓ npm install OK[/]")
                        else:
                            console.print(f"    [red]✗ failed: {out.stderr[:200]}[/]")
                    except Exception as e:
                        console.print(f"    [red]✗ {e}[/]")
                else:
                    console.print(f"  [dim]- 不能自动修: {issue[:80]}[/]")
        else:
            console.print("[dim]提示: 加 --fix 让 KUN 自动修能修的 (alembic / npm install)[/]")


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
    """Run one task directly via the orchestrator (CLI client, no HTTP).

    V2.3 装上协议消费 + 反作弊 + Predictive Coding hook (走 install_runtime).
    KUN_QI_RUNTIME_ENABLED=0 强制关闭整套.
    """
    from types import SimpleNamespace

    from starlette.datastructures import State

    from kun.api.runtime import install_runtime
    from kun.core.logging import configure_logging
    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.watchtower.engine import RuleEngine

    configure_logging()

    async def _run() -> None:
        # 跟 server 同样初始化 — V2.3 协议消费 + 反作弊 + PC hook 全装上
        app = SimpleNamespace(state=State())
        install_runtime(app, rule_engine=RuleEngine([]))
        orch = app.state.orchestrator
        # CLI 模式跑前 seed 5 个 starter protocol (server 是 lifespan 里 seed)
        registry = getattr(app.state, "protocol_registry", None)
        if registry is not None:
            from kun.qi.seed_protocols import seed_default_protocols

            await seed_default_protocols(registry)
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


@promises_app.command("generate")
def promises_generate(
    rev_range: str = typer.Option("HEAD~20..HEAD", "--range", help="git rev range"),
    output: Path = typer.Option(Path("docs/PROMISES.md"), "--output"),
    title: str = typer.Option("自动生成承诺更新", "--title"),
    write: bool = typer.Option(False, "--write", help="追加写入 PROMISES.md；默认只打印"),
) -> None:
    """Generate a PROMISES.md section from git log subjects."""

    from kun.engineering.promises_autogen import (
        append_promises_section,
        generate_promises_section,
    )

    section = generate_promises_section(rev_range=rev_range, cwd=Path.cwd(), title=title)
    if write:
        append_promises_section(output, section)
        console.print(f"[green]PROMISES section appended[/]: {output}")
        return
    console.print(section)


@release_app.command("notes")
def release_notes(
    rev_range: str = typer.Option("v2.1.0..HEAD", "--range", help="git rev range"),
    output: Path = typer.Option(Path("CHANGELOG-v2.2.md"), "--output"),
    version: str = typer.Option("v2.2.0", "--version"),
    stdout: bool = typer.Option(False, "--stdout", help="只打印，不写文件"),
) -> None:
    """Generate V2.2 release notes from git log subjects."""

    from kun.engineering.promises_autogen import generate_release_notes

    notes = generate_release_notes(rev_range=rev_range, cwd=Path.cwd(), version=version)
    if stdout:
        console.print(notes)
        return
    output.write_text(notes)
    console.print(f"[green]Release notes written[/]: {output}")


@ops_app.command("preflight")
def ops_preflight(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="有 blocker 时返回非零退出码",
    ),
    skip_alembic: bool = typer.Option(False, "--skip-alembic", help="跳过 alembic heads 检查"),
) -> None:
    """上线前硬检查：配置、迁移、备份脚本、能力边界诚实性。"""

    from kun.ops.preflight import run_preflight

    report = run_preflight(run_alembic_heads=not skip_alembic)
    if json_output:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        table = Table(title=f"KUN production preflight — {report.status}")
        table.add_column("severity")
        table.add_column("check")
        table.add_column("detail")
        table.add_column("action")
        color = {"ok": "green", "warn": "yellow", "blocker": "red"}
        for check in report.checks:
            table.add_row(
                f"[{color[check.severity]}]{check.severity}[/]",
                check.title,
                check.detail,
                check.suggested_action or "-",
            )
        console.print(table)
        console.print(
            "[dim]delivery summary: "
            + json.dumps(report.delivery_summary, ensure_ascii=False)
            + "[/]"
        )
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


@ops_app.command("delivery-status")
def ops_delivery_status(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    fail_on_issue: bool = typer.Option(
        True,
        "--fail-on-issue/--no-fail-on-issue",
        help="能力边界标注不诚实时返回非零退出码",
    ),
    fail_on_not_ready: bool = typer.Option(
        False,
        "--fail-on-not-ready",
        help="仍有用户可见能力不是 ready 时返回非零退出码；适合 release gate",
    ),
) -> None:
    """查看 KUN 现在到底哪些能力真能承诺，哪些还只是 partial。"""

    from kun.engineering.delivery_status import (
        delivery_status_summary,
        get_v3_delivery_status,
        validate_delivery_status,
    )

    items = get_v3_delivery_status()
    issues = validate_delivery_status(items)
    payload = {
        "items": [item.model_dump(mode="json") for item in items],
        "summary": delivery_status_summary(),
        "validation_issues": issues,
    }
    if json_output:
        console.print_json(data=payload)
    else:
        table = Table(title="KUN delivery status — 诚实能力边界")
        table.add_column("status")
        table.add_column("capability")
        table.add_column("summary")
        table.add_column("missing")
        color = {
            "ready": "green",
            "partial": "yellow",
            "audit_only": "cyan",
            "not_ready": "red",
        }
        for item in items:
            missing = "；".join(item.missing[:3])
            if len(item.missing) > 3:
                missing += f"；另 {len(item.missing) - 3} 项"
            table.add_row(
                f"[{color[item.status]}]{item.status}[/]",
                item.label,
                item.summary,
                missing or "-",
            )
        console.print(table)
        console.print("[dim]summary: " + json.dumps(payload["summary"], ensure_ascii=False) + "[/]")
        if issues:
            console.print("[red]validation issues:[/]")
            for issue in issues:
                console.print(f"  - {issue}")
    if fail_on_issue and issues:
        raise typer.Exit(code=2)
    if fail_on_not_ready and any(item.user_visible and item.status != "ready" for item in items):
        raise typer.Exit(code=3)


@ops_app.command("secret-audit")
def ops_secret_audit(
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="发现生产级 blocker 时返回非零退出码",
    ),
) -> None:
    """检查密钥、默认账号、真实外部 handler 配置是否安全。"""

    from kun.ops.secret_audit import audit_runtime_secrets

    report = audit_runtime_secrets()
    if json_output:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        table = Table(title=f"KUN secret/config audit — {report.status}")
        table.add_column("severity")
        table.add_column("area")
        table.add_column("finding")
        table.add_column("action")
        color = {"ok": "green", "warn": "yellow", "blocker": "red"}
        for item in report.items:
            table.add_row(
                f"[{color[item.severity]}]{item.severity}[/]",
                item.area,
                item.title,
                item.suggested_action or "-",
            )
        console.print(table)
        console.print("[dim]summary: " + json.dumps(report.summary, ensure_ascii=False) + "[/]")
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


@ops_app.command("secret-store-set")
def ops_secret_store_set(
    name: str = typer.Option(..., "--name", help="只允许 KUN_WORLD_* 配置名"),
    value: str = typer.Option(..., "--value", help="要写入的密钥/配置值；不会在输出中回显"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；为空则写 global"),
    store_file: Path | None = typer.Option(
        None,
        "--file",
        help="secret store JSON 路径；默认读取 KUN_SECRET_STORE_FILE",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """写入本地 JSON secret store。

    这是 file-backed 桥，不是云 KMS；只用于 WorldGateway 的 KUN_WORLD_* 配置。
    """

    import os

    from kun.ops.secret_store import SECRET_STORE_FILE_ENV, upsert_secret_store_value

    path = store_file
    if path is None:
        raw = os.environ.get(SECRET_STORE_FILE_ENV, "").strip()
        if raw:
            path = Path(raw)
    if path is None:
        console.print("[red]需要 --file 或 KUN_SECRET_STORE_FILE[/]")
        raise typer.Exit(code=2)
    try:
        result = upsert_secret_store_value(
            path=path,
            tenant_id=tenant,
            name=name,
            value=value,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    payload = {
        "path": result.path,
        "scope": result.scope,
        "tenant_id": result.tenant_id,
        "name": result.name,
        "tenant_count": result.tenant_count,
        "global_key_count": result.global_key_count,
        "honest_limits": list(result.honest_limits),
    }
    if json_output:
        console.print_json(data=payload)
    else:
        console.print(
            f"[green]secret store updated[/]: {result.scope} {result.tenant_id or 'global'} "
            f"{result.name} -> {result.path}"
        )
        console.print("[dim]value hidden; this is not cloud KMS or automatic rotation.[/]")


@ops_app.command("backup-drill-create")
def ops_backup_drill_create(
    repo_root: Path = typer.Option(Path.cwd(), "--repo-root", help="KUN repo root"),
    output_dir: Path = typer.Option(Path("backups"), "--output-dir", help="备份包输出目录"),
    source: list[Path] | None = typer.Option(
        None,
        "--source",
        help="要打包的路径；可重复传。默认使用 KUN 关键配置/运维路径。",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """创建本地备份演练包 + manifest。

    这是本地文件级 drill，不等于云备份；真实数据库仍要跑
    scripts/backup_postgres.sh 和 restore_postgres_smoke.sh。
    """

    from kun.ops.backup_restore import (
        create_backup_package,
        default_allowed_roots,
        default_backup_sources,
    )

    root = repo_root.resolve()
    sources = source or default_backup_sources(root)
    allowed = default_allowed_roots(root)
    if source:
        allowed.extend(raw.resolve() for raw in source)
    try:
        manifest = create_backup_package(
            source_paths=sources,
            output_dir=output_dir,
            repo_root=root,
            allowed_roots=allowed,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    payload = manifest.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
        return
    console.print(
        f"[green]backup drill package created[/]: {manifest.archive_path} "
        f"({manifest.file_count} files, {manifest.total_bytes} bytes)"
    )
    console.print("[dim]manifest is next to the archive; run backup-drill-restore-dry-run.[/]")


@ops_app.command("backup-drill-restore-dry-run")
def ops_backup_drill_restore_dry_run(
    manifest: Path = typer.Argument(..., help="*.manifest.json from backup-drill-create"),
    restore_root: Path = typer.Option(
        Path(".kun-restore-dry-run"),
        "--restore-root",
        help="dry-run 目标目录；不会写入文件，只检查覆盖/缺失/校验。",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="校验失败时返回非零退出码",
    ),
) -> None:
    """验证备份包可恢复，但不写入/覆盖任何文件。"""

    from kun.ops.backup_restore import restore_dry_run

    report = restore_dry_run(manifest_path=manifest, restore_root=restore_root)
    payload = report.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
    else:
        color = {"pass": "green", "warn": "yellow", "block": "red"}[report.status]
        console.print(
            f"[{color}]restore dry-run {report.status}[/]: "
            f"{report.would_restore_count}/{report.file_count} files would restore"
        )
        if report.would_overwrite:
            console.print(f"[yellow]would overwrite[/]: {', '.join(report.would_overwrite[:5])}")
        if report.missing_from_archive:
            console.print(f"[red]missing[/]: {', '.join(report.missing_from_archive[:5])}")
        if report.sha256_mismatches:
            console.print(f"[red]sha256 mismatch[/]: {', '.join(report.sha256_mismatches[:5])}")
        if report.notes:
            console.print("[dim]" + "；".join(report.notes) + "[/]")
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


@ops_app.command("readiness")
def ops_readiness(
    tenant: str = typer.Option("u-sylvan", "--tenant", help="租户 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    include_dogfood: bool = typer.Option(
        False,
        "--include-dogfood",
        help="同时跑低风险 dogfood；默认只跑静态/轻量门禁",
    ),
    include_db_mission: bool = typer.Option(
        False,
        "--include-db-mission",
        help="dogfood 里额外跑真实数据库 Mission 续跑 smoke",
    ),
    include_db_account: bool = typer.Option(
        False,
        "--include-db-account",
        help="dogfood 里额外跑真实数据库账号账本 / session / 成员邀请 smoke",
    ),
    include_db_state_ledger_repair: bool = typer.Option(
        False,
        "--include-db-state-ledger-repair",
        help="dogfood 里额外跑真实数据库 StateLedger repair smoke",
    ),
    include_db_long_horizon_drill: bool = typer.Option(
        False,
        "--include-db-long-horizon-drill",
        help="dogfood 里额外跑时间压缩的多轮长期 Mission drill",
    ),
    skip_alembic: bool = typer.Option(False, "--skip-alembic", help="跳过 alembic heads 检查"),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="发现 blocker 时返回非零退出码",
    ),
) -> None:
    """正式测试前的一条命令：preflight + secret-audit + delivery + 可选 dogfood。"""

    from kun.ops.readiness import run_readiness_report

    report = asyncio.run(
        run_readiness_report(
            tenant_id=tenant,
            include_dogfood=include_dogfood,
            include_db_mission=include_db_mission,
            include_db_account=include_db_account,
            include_db_state_ledger_repair=include_db_state_ledger_repair,
            include_db_long_horizon_drill=include_db_long_horizon_drill,
            run_alembic_heads=not skip_alembic,
        )
    )
    payload = report.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
    else:
        table = Table(title=f"KUN readiness — {report.status}")
        table.add_column("section")
        table.add_column("status")
        table.add_column("summary")
        table.add_row("preflight", report.preflight.status, str(report.preflight.delivery_summary))
        table.add_row("secret-audit", report.secret_audit.status, str(report.secret_audit.summary))
        table.add_row("delivery", "-", json.dumps(report.delivery_summary, ensure_ascii=False))
        if report.dogfood is not None:
            table.add_row(
                "dogfood",
                report.dogfood.status,
                f"{len(report.dogfood.scenarios)} scenarios",
            )
        console.print(table)
        if report.blockers:
            console.print("[red]blockers:[/]")
            for item in report.blockers[:12]:
                console.print(f"  - {item}")
        if report.next_steps:
            console.print("[dim]next:[/]")
            for item in report.next_steps:
                console.print(f"  - {item}")
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


@ops_app.command("release-check")
def ops_release_check(
    tag: str = typer.Option(..., "--tag", help="准备创建的 release tag，例如 v4.0.0"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="允许脏工作区，只给 warning"),
    skip_git: bool = typer.Option(False, "--skip-git", help="跳过 git dirty/tag 检查"),
    skip_alembic: bool = typer.Option(False, "--skip-alembic", help="跳过 alembic heads 检查"),
    require_ready: bool = typer.Option(
        False,
        "--require-ready",
        help="要求所有用户可见能力都是 ready；适合正式生产 release",
    ),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="有 blocker 时返回非零退出码",
    ),
) -> None:
    """检查能不能打 release tag：tag、rollback、hotfix、preflight、legal guard。"""

    from kun.ops.release_gate import run_release_gate

    report = run_release_gate(
        release_tag=tag,
        allow_dirty=allow_dirty,
        run_git=not skip_git,
        run_alembic_heads=not skip_alembic,
        require_ready=require_ready,
    )
    if json_output:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        table = Table(title=f"KUN release gate — {tag} — {report.status}")
        table.add_column("severity")
        table.add_column("check")
        table.add_column("detail")
        table.add_column("action")
        color = {"ok": "green", "warn": "yellow", "blocker": "red"}
        for check in report.checks:
            table.add_row(
                f"[{color[check.severity]}]{check.severity}[/]",
                check.title,
                check.detail,
                check.suggested_action or "-",
            )
        console.print(table)
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


@ops_app.command("credit-report")
def ops_credit_report(
    tenant: str = typer.Option("u-sylvan", "--tenant", help="租户 ID"),
    kind: str | None = typer.Option(None, "--kind", help="memory/skill/model 等资源类型过滤"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """查看哪些 memory / skill / model / 决策资源真的贡献过结果。"""

    from kun.core.db import session_scope
    from kun.engineering.credit_assignment import load_top_resource_credit

    async def _load() -> list[dict[str, Any]]:
        async with session_scope(tenant_id=tenant) as s:
            rows = await load_top_resource_credit(
                s,
                tenant_id=tenant,
                resource_kind=kind,
                limit=limit,
            )
        return [item.model_dump(mode="json") for item in rows]

    rows = asyncio.run(_load())
    if json_output:
        console.print_json(data={"items": rows})
        return
    table = Table(title=f"KUN resource credit — {tenant}")
    table.add_column("score")
    table.add_column("kind")
    table.add_column("resource")
    table.add_column("used/pass/critical")
    table.add_column("credit")
    for row in rows:
        table.add_row(
            f"{float(row['contribution_score']):.2f}",
            str(row["resource_kind"]),
            str(row["resource_id"]),
            f"{row['used_count']}/{row['pass_count']}/{row['critical_count']}",
            f"{float(row['credit_total']):.2f}",
        )
    console.print(table)


@ops_app.command("state-ledger-repair")
def ops_state_ledger_repair(
    tenant: str = typer.Option("u-sylvan", "--tenant", help="租户 ID"),
    task_id: str = typer.Option(..., "--task-id", help="要修复/预览的 task_id"),
    user: str | None = typer.Option(None, "--user", help="可选 user_id 过滤"),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="真正写回 state_ledger_entries；默认只 dry-run",
    ),
    limit: int = typer.Option(300, "--limit", min=10, max=1000, help="最多回放多少条事件"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """从 EventRow 长期事件账本重建 State Ledger 当前快照。

    默认是 dry-run，只告诉你当前快照和回放快照差哪里。带 --apply 才写回。
    """

    from kun.ops.state_ledger_repair import repair_state_ledger_snapshot

    result = asyncio.run(
        repair_state_ledger_snapshot(
            tenant_id=tenant,
            task_id=task_id,
            user_id=user,
            apply=apply,
            limit=limit,
        )
    )
    payload = result.model_dump(mode="json")
    if json_output:
        console.print_json(data=payload)
        return
    title = "KUN State Ledger repair"
    if apply:
        title += " — applied"
    table = Table(title=title)
    table.add_column("field")
    table.add_column("current")
    table.add_column("replayed")
    for diff in result.diffs:
        table.add_row(diff.field, str(diff.current), str(diff.replayed))
    console.print(table)
    console.print(
        "[dim]summary: "
        + json.dumps(
            {
                "tenant_id": result.tenant_id,
                "task_id": result.task_id,
                "reason": result.reason,
                "applied": result.applied,
                "event_count": result.event_count,
                "reconstruction_confidence": result.reconstruction_confidence,
                "gaps": result.gaps,
                "diff_count": len(result.diffs),
            },
            ensure_ascii=False,
        )
        + "[/]"
    )


@ops_app.command("onboard-tenant")
def ops_onboard_tenant(
    tenant: str = typer.Option(..., "--tenant", help="租户 ID"),
    user: str = typer.Option("", "--user", help="可选 user_id"),
    scopes: str = typer.Option(
        "world:approve,world:dispatch",
        "--scopes",
        help="逗号分隔权限 scope",
    ),
    audience: str = typer.Option("developer", "--audience", help="novice/developer/expert"),
    ttl_sec: int = typer.Option(86400, "--ttl-sec", min=60),
    api_origin: str = typer.Option("http://localhost:8000", "--api-origin"),
    output: Path | None = typer.Option(None, "--output", help="写入 JSON 文件"),
) -> None:
    """生成一个租户启动包：签名 token + smoke curl + 边界说明。"""

    import os

    from kun.ops.tenant_onboarding import create_tenant_onboarding_pack

    secret = os.environ.get("KUN_AUTH_SECRET", "")
    if not secret:
        secret = next(
            (
                item.strip()
                for item in os.environ.get("KUN_AUTH_SECRETS", "").split(",")
                if len(item.strip()) >= 32
            ),
            "",
        )
    if audience not in {"novice", "developer", "expert"}:
        console.print("[red]audience 必须是 novice/developer/expert[/]")
        raise typer.Exit(code=2)
    try:
        pack = create_tenant_onboarding_pack(
            tenant_id=tenant,
            user_id=user or None,
            scopes=[item.strip() for item in scopes.split(",") if item.strip()],
            audience=audience,  # type: ignore[arg-type]
            ttl_sec=ttl_sec,
            api_origin=api_origin,
            secret=secret,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    payload = pack.model_dump(mode="json")
    if output is not None:
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]tenant onboarding pack written[/]: {output}")
    else:
        console.print_json(data=payload)


@ops_app.command("account-bootstrap")
def ops_account_bootstrap(
    tenant: str = typer.Option(..., "--tenant", help="租户 ID"),
    organization: str = typer.Option("", "--org", help="组织 ID；默认等于 tenant"),
    name: str = typer.Option("", "--name", help="租户展示名；默认等于 tenant"),
    owner: str = typer.Option(..., "--owner", help="owner user_id"),
    scopes: str = typer.Option(
        "world:approve,world:dispatch",
        "--scopes",
        help="逗号分隔权限 scope",
    ),
    audience: str = typer.Option("developer", "--audience", help="novice/developer/expert"),
    plan: str = typer.Option("dev", "--plan"),
    billing_status: str = typer.Option("manual", "--billing-status"),
    ttl_sec: int = typer.Option(86400, "--ttl-sec", min=60),
    persist: bool = typer.Option(
        True,
        "--persist/--dry-run",
        help="默认写入 tenant_accounts / tenant_members / token ledger；--dry-run 只生成包",
    ),
    output: Path | None = typer.Option(None, "--output", help="写入 JSON 文件"),
) -> None:
    """生成账号/组织启动包，并可写入租户账本。

    这是 V4 的第一版生产账号底座：有组织/成员/token 签发记录，但还不是
    完整自助注册、登录、账单系统。
    """

    import os

    from kun.core.db import session_scope
    from kun.ops.account_registry import (
        bootstrap_tenant_account,
        build_unpersisted_bootstrap,
    )

    secret = os.environ.get("KUN_AUTH_SECRET", "")
    if not secret:
        secret = next(
            (
                item.strip()
                for item in os.environ.get("KUN_AUTH_SECRETS", "").split(",")
                if len(item.strip()) >= 32
            ),
            "",
        )
    if audience not in {"novice", "developer", "expert"}:
        console.print("[red]audience 必须是 novice/developer/expert[/]")
        raise typer.Exit(code=2)
    cleaned_scopes = [item.strip() for item in scopes.split(",") if item.strip()]
    organization_id = organization.strip() or tenant
    display_name = name.strip() or tenant

    async def _persist() -> dict[str, Any]:
        async with session_scope(tenant_id=tenant) as s:
            pack = await bootstrap_tenant_account(
                s,
                tenant_id=tenant,
                organization_id=organization_id,
                display_name=display_name,
                owner_user_id=owner,
                scopes=cleaned_scopes,
                audience=audience,  # type: ignore[arg-type]
                plan=plan,
                billing_status=billing_status,
                ttl_sec=ttl_sec,
                secret=secret,
            )
        return pack.model_dump(mode="json")

    try:
        if persist:
            payload = asyncio.run(_persist())
        else:
            payload = build_unpersisted_bootstrap(
                tenant_id=tenant,
                organization_id=organization_id,
                display_name=display_name,
                owner_user_id=owner,
                scopes=cleaned_scopes,
                audience=audience,  # type: ignore[arg-type]
                plan=plan,
                billing_status=billing_status,
                ttl_sec=ttl_sec,
                secret=secret,
            ).model_dump(mode="json")
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if output is not None:
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]tenant account bootstrap written[/]: {output}")
    else:
        console.print_json(data=payload)


@ops_app.command("revoke-token")
def ops_revoke_token(
    tenant: str = typer.Option(..., "--tenant", help="租户 ID"),
    token_id: str = typer.Option(..., "--token-id", help="account-bootstrap 返回的 token_id"),
    reason: str = typer.Option("", "--reason", help="撤销原因，只存摘要不存 token"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
) -> None:
    """撤销租户 token 签发记录。

    API production middleware 会拒绝通过账号账本签发且已 revoked 的 token。
    """

    from kun.core.db import session_scope
    from kun.ops.account_registry import revoke_token_issue

    async def _revoke() -> bool:
        async with session_scope(tenant_id=tenant) as s:
            return await revoke_token_issue(
                s,
                tenant_id=tenant,
                token_id=token_id,
                reason=reason,
            )

    revoked = asyncio.run(_revoke())
    payload = {"tenant_id": tenant, "token_id": token_id, "revoked": revoked}
    if json_output:
        console.print_json(data=payload)
    elif revoked:
        console.print(f"[green]token revoked[/]: {tenant}/{token_id}")
    else:
        console.print(f"[yellow]token not found or already revoked[/]: {tenant}/{token_id}")
    if not revoked:
        raise typer.Exit(code=1)


@ops_app.command("account-session")
def ops_account_session(
    tenant: str = typer.Option(..., "--tenant", help="租户 ID"),
    user: str = typer.Option(..., "--user", help="user_id"),
    scopes: str = typer.Option(
        "chat:write,world:approve",
        "--scopes",
        help="逗号分隔权限 scope",
    ),
    audience: str = typer.Option("developer", "--audience", help="novice/developer/expert"),
    access_ttl_sec: int = typer.Option(900, "--access-ttl-sec", min=60),
    refresh_ttl_sec: int = typer.Option(2_592_000, "--refresh-ttl-sec", min=300),
    output: Path | None = typer.Option(None, "--output", help="写入 JSON 文件"),
) -> None:
    """签发最小 access+refresh 会话包，并写入 token 账本。

    这不是登录/OAuth。它是 operator 管理阶段的真实入口：管理员先用
    account-bootstrap 建好租户，再用这里签发可撤销、可审计、可续期的会话 token。
    """

    import os

    from kun.core.db import session_scope
    from kun.ops.account_sessions import issue_session_token_pair

    secret = os.environ.get("KUN_AUTH_SECRET", "")
    if not secret:
        secret = next(
            (
                item.strip()
                for item in os.environ.get("KUN_AUTH_SECRETS", "").split(",")
                if len(item.strip()) >= 32
            ),
            "",
        )
    if audience not in {"novice", "developer", "expert"}:
        console.print("[red]audience 必须是 novice/developer/expert[/]")
        raise typer.Exit(code=2)
    cleaned_scopes = [item.strip() for item in scopes.split(",") if item.strip()]

    async def _issue() -> dict[str, Any]:
        async with session_scope(tenant_id=tenant) as s:
            pair = await issue_session_token_pair(
                s,
                tenant_id=tenant,
                user_id=user,
                scopes=cleaned_scopes,
                audience=audience,  # type: ignore[arg-type]
                access_ttl_sec=access_ttl_sec,
                refresh_ttl_sec=refresh_ttl_sec,
                secret=secret,
            )
        return pair.model_dump(mode="json")

    try:
        payload = asyncio.run(_issue())
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if output is not None:
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]tenant account session written[/]: {output}")
    else:
        console.print_json(data=payload)


@ops_app.command("dogfood")
def ops_dogfood(
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    include_db_mission: bool = typer.Option(
        False,
        "--include-db-mission",
        help="额外跑真实数据库 Mission 续跑 smoke；需要本地 Postgres/Alembic 可用",
    ),
    include_db_account: bool = typer.Option(
        False,
        "--include-db-account",
        help="额外跑真实数据库账号账本 / session / 成员邀请 smoke；需要本地 Postgres/Alembic 可用",
    ),
    include_db_state_ledger_repair: bool = typer.Option(
        False,
        "--include-db-state-ledger-repair",
        help="额外跑真实数据库 StateLedger repair smoke；需要本地 Postgres/Alembic 可用",
    ),
    include_db_long_horizon_drill: bool = typer.Option(
        False,
        "--include-db-long-horizon-drill",
        help="额外跑时间压缩的多轮长期 Mission drill；需要本地 Postgres/Alembic 可用",
    ),
    fail_on_blocker: bool = typer.Option(
        True,
        "--fail-on-blocker/--no-fail-on-blocker",
        help="有 blocker 时返回非零退出码",
    ),
) -> None:
    """跑 V4 低风险 dogfood：preflight、租户 token、WorldGateway、诚实状态。"""

    from kun.ops.dogfood import run_v4_dogfood

    report = asyncio.run(
        run_v4_dogfood(
            tenant_id=tenant,
            include_db_mission=include_db_mission,
            include_db_account=include_db_account,
            include_db_state_ledger_repair=include_db_state_ledger_repair,
            include_db_long_horizon_drill=include_db_long_horizon_drill,
        )
    )
    if json_output:
        console.print_json(data=report.model_dump(mode="json"))
    else:
        table = Table(title=f"KUN V4 dogfood — {report.status}")
        table.add_column("status")
        table.add_column("scenario")
        table.add_column("summary")
        table.add_column("next")
        color = {"pass": "green", "warn": "yellow", "block": "red"}
        for item in report.scenarios:
            table.add_row(
                f"[{color[item.status]}]{item.status}[/]",
                item.scenario_id,
                item.summary,
                item.next_step or "-",
            )
        console.print(table)
    if fail_on_blocker and report.status == "block":
        raise typer.Exit(code=2)


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


@protocol_app.command("list")
def protocol_list(
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """List ProtocolRegistry entries for a tenant.

    CLI 模式 InMemory storage 是 per-process — 每次启都空, 自动 seed 5 个 starter.
    """
    from kun.qi import get_protocol_registry
    from kun.qi.seed_protocols import seed_default_protocols

    async def _go() -> None:
        registry = get_protocol_registry()
        # 自动 seed (idempotent — 已存在跳过)
        await seed_default_protocols(registry)
        protocols = await registry.list_all(tenant)
        table = Table(title=f"protocols — {tenant}")
        table.add_column("protocol_id")
        table.add_column("version")
        table.add_column("status")
        table.add_column("pattern")
        table.add_column("mode")
        for p in protocols:
            table.add_row(
                p.protocol_id,
                p.version,
                p.status,
                p.trigger.task_type_pattern,
                p.execution.mode,
            )
        console.print(table)

    asyncio.run(_go())


@protocol_app.command("save")
def protocol_save(
    path: Path = typer.Argument(..., help="Protocol JSON file"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Save a Protocol JSON file into the registry."""
    from kun.qi import Protocol, get_protocol_registry

    raw_protocol = path.read_text()

    async def _go() -> None:
        protocol = Protocol.model_validate_json(raw_protocol)
        protocol = protocol.model_copy(update={"tenant_id": tenant})
        await get_protocol_registry().save(protocol)
        console.print(
            f"[green]saved[/] {protocol.protocol_id}@{protocol.version} status={protocol.status}"
        )

    asyncio.run(_go())


@protocol_app.command("get")
def protocol_get(
    protocol_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Print one protocol as JSON."""
    from kun.qi import get_protocol_registry

    async def _go() -> None:
        protocol = await get_protocol_registry().get(tenant, protocol_id, version)
        if protocol is None:
            console.print(f"[red]not found[/] {protocol_id}@{version}")
            raise typer.Exit(code=2)
        console.print_json(protocol.model_dump_json())

    asyncio.run(_go())


@protocol_app.command("promote")
def protocol_promote(
    protocol_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    status: str = typer.Argument(..., help="shadow | canary | stable | rolled_back"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Promote a protocol through its lifecycle."""
    from kun.qi import ProtocolStatus, get_protocol_registry

    async def _go() -> None:
        await get_protocol_registry().promote(
            tenant, protocol_id, version, cast(ProtocolStatus, status)
        )
        console.print(f"[green]promoted[/] {protocol_id}@{version} → {status}")

    asyncio.run(_go())


@protocol_app.command("rollback")
def protocol_rollback(
    protocol_id: str = typer.Argument(...),
    version: str = typer.Argument(...),
    reason: str = typer.Option("", "--reason"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Rollback a protocol version."""
    from kun.qi import get_protocol_registry

    async def _go() -> None:
        await get_protocol_registry().rollback(tenant, protocol_id, version, reason=reason)
        console.print(f"[yellow]rolled_back[/] {protocol_id}@{version}")

    asyncio.run(_go())


@protocol_app.command("match")
def protocol_match(
    task_type: str = typer.Argument(...),
    complexity: float = typer.Option(0.5, "--complexity"),
    risk: str = typer.Option("low", "--risk"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Find the stable protocol that would handle a task."""
    from kun.qi import get_protocol_registry

    async def _go() -> None:
        protocol = await get_protocol_registry().find_protocol_for(
            {
                "task_type": task_type,
                "complexity_score": complexity,
                "risk_level": risk,
            },
            tenant,
        )
        if protocol is None:
            console.print("[yellow]no matching stable protocol[/]")
            return
        console.print_json(protocol.model_dump_json())

    asyncio.run(_go())


@qi_app.command("status")
def qi_status(
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """Show 启 (Qi) window status + today's spend."""
    import os

    from kun.qi import get_qi_budget
    from kun.qi.window import QiWindowConfig, is_qi_window_active

    cfg = QiWindowConfig()
    forced = os.getenv("KUN_QI_FORCE_ACTIVE", "0") == "1"
    # V2.3: KUN_QI_ENABLED default ON (跟 install_runtime 一致)
    enabled = os.getenv("KUN_QI_ENABLED", "1") == "1"
    active = enabled and (forced or is_qi_window_active(cfg))
    budget = get_qi_budget()

    console.print(f"[bold cyan]启 (Qi) status[/] — tenant={tenant}")
    console.print(f"  KUN_QI_ENABLED      = {os.getenv('KUN_QI_ENABLED', '1')}")
    console.print(f"  KUN_QI_FORCE_ACTIVE = {os.getenv('KUN_QI_FORCE_ACTIVE', '0')}")
    console.print(f"  window_active       = {'[green]yes[/]' if active else '[yellow]no[/]'}")
    spent = budget.get_today_spent(tenant)
    remaining = budget.remaining_budget(tenant)
    console.print(f"  daily_limit_usd     = {budget._daily_limit:.2f}")
    console.print(f"  spent_today_usd     = {spent:.4f}")
    console.print(f"  remaining_usd       = {remaining:.4f}")


@qi_app.command("start")
def qi_start(
    duration_minutes: int = typer.Option(60, "--duration", help="Force active window length"),
    daily_budget_usd: float = typer.Option(5.0, "--budget", help="Daily budget cap (USD)"),
) -> None:
    """Force 启 window active for N minutes (sets KUN_QI_FORCE_ACTIVE in process env)."""
    import os

    os.environ["KUN_QI_ENABLED"] = "1"
    os.environ["KUN_QI_FORCE_ACTIVE"] = "1"
    os.environ["KUN_QI_DAILY_BUDGET_USD"] = str(daily_budget_usd)
    console.print(
        f"[green]启 force-active[/] for {duration_minutes} min, budget={daily_budget_usd}USD"
    )
    console.print(
        "[yellow]Note:[/] only this CLI process sees the env. For server, set env before `kun serve`."
    )


@qi_app.command("stop")
def qi_stop() -> None:
    """Stop 启 (Qi) — clear KUN_QI_FORCE_ACTIVE for this CLI process."""
    import os

    os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
    os.environ["KUN_QI_ENABLED"] = "0"
    console.print("[green]启 stopped[/] for this CLI process.")


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


@security_app.command("task-boundary-benchmark")
def security_task_boundary_benchmark(
    dataset: Path | None = typer.Option(
        None,
        "--dataset",
        help="OffTopicEval-compatible JSONL/JSON dataset. Default: bundled smoke dataset.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print full JSON report."),
) -> None:
    """Run TaskBoundaryGuard benchmark (OffTopicEval-compatible)."""
    from kun.security.task_boundary_benchmark import (
        load_dataset,
        load_default_dataset,
        run_benchmark,
    )
    from kun.security.task_boundary_guard import TaskBoundaryGuard

    async def _go() -> None:
        if dataset is None:
            bundle = load_default_dataset()
            dataset_name = bundle.name
            cases = bundle.cases
        else:
            dataset_name = dataset.stem
            cases = load_dataset(dataset)

        report = await run_benchmark(
            TaskBoundaryGuard(),
            cases,
            dataset_name=dataset_name,
        )
        if json_output:
            console.print_json(report.model_dump_json())
            return

        table = Table(title=f"task-boundary benchmark — {report.dataset}")
        table.add_column("total", justify="right")
        table.add_column("accuracy", justify="right")
        table.add_column("reject_rate", justify="right")
        table.add_column("off_topic_reject", justify="right")
        table.add_column("false_accept", justify="right")
        table.add_column("false_reject", justify="right")
        table.add_row(
            str(report.total),
            f"{report.accuracy:.2f}",
            f"{report.reject_rate:.2f}",
            f"{report.off_topic_reject_rate:.2f}",
            str(report.false_accept_count),
            str(report.false_reject_count),
        )
        console.print(table)
        for result in report.results:
            if not result.passed:
                console.print(
                    f"[red]{result.case_id}[/] expected={result.expected_in_scope} "
                    f"actual={result.actual_in_scope} reason={result.reason}"
                )

    asyncio.run(_go())


# =============== lab subcommands (Wire 22) ===============


@lab_app.command("run")
def lab_run(
    task: str = typer.Argument(..., help="Task prompt for ensemble experiment"),
    paths: int = typer.Option(5, "--paths", "-n", min=2, max=10, help="并发路径数"),
    selection: str = typer.Option(
        "best_score",
        "--selection",
        help="best_score | majority_vote | judge_picks",
    ),
    task_type: str = typer.Option("kun_lab.cli", "--task-type", help="跟主仓库 task taxonomy 对齐"),
    enable: bool = typer.Option(
        False,
        "--enable",
        help="自动 set KUN_LAB_MODE=1 (默认要求用户已 export)",
    ),
    no_emit: bool = typer.Option(False, "--no-emit", help="不 emit experiment.created 事件"),
    cost_budget: float = typer.Option(1.0, "--cost-budget", help="lab 总预算上限 USD"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """跑一次 ensemble 实验 (V2.2 §26.3 HEX 启发).

    示例:
        kun lab run "为 Q4 商业方案出 3 套" --paths 5 --enable
    """
    import os

    if enable:
        os.environ["KUN_LAB_MODE"] = "1"

    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.lab import (
        EnsembleConfig,
        EnsembleExecutor,
        LabEventEmitter,
        get_experiment_log,
        make_default_adapter,
    )
    from kun.lab.ensemble_executor import is_lab_enabled

    if not is_lab_enabled():
        console.print("[bold red]KUN-Lab 未启用[/]: export KUN_LAB_MODE=1, 或加 --enable flag")
        raise typer.Exit(code=2)

    async def _go() -> None:
        adapter = make_default_adapter(task_type=task_type)
        emitter = None if no_emit else LabEventEmitter(task_type_default=task_type)
        executor = EnsembleExecutor(
            adapter,
            event_emitter=emitter.on_experiment_completed if emitter else None,
        )
        cfg = EnsembleConfig(
            n_paths=paths,
            selection_method=selection,
            cost_budget_total_usd=cost_budget,
            metadata={"prompt": task},
        )

        with tenant_scope(TenantContext(tenant_id=tenant)):
            result = await executor.run(task, config=cfg, task_type=task_type)
            # 同时记进 ExperimentLog 让 promote 子命令能看
            get_experiment_log().record(
                task_type=task_type,
                ensemble_result=result,
                prompt_hash=_hash_prompt(task),
            )

        # 输出
        table = Table(title=f"实验 {result.experiment_id} — winner={result.winning_path_idx}")
        table.add_column("idx", justify="right")
        table.add_column("strategy")
        table.add_column("tier")
        table.add_column("score", justify="right")
        table.add_column("cost $", justify="right")
        table.add_column("err")
        for pr in result.path_results:
            table.add_row(
                str(pr.path_idx),
                str(pr.config.get("strategy", "")),
                str(pr.config.get("tier", "")),
                f"{pr.score:.2f}",
                f"{pr.cost_usd:.4f}",
                "[red]" + pr.error[:40] + "[/]" if pr.error else "",
            )
        console.print(table)
        console.print(
            f"[bold green]winning_output[/]: {result.winning_output[:200]!r}"
            + ("..." if len(result.winning_output) > 200 else "")
        )
        console.print(
            f"selection={result.selection_reason}  cost=${result.total_cost_usd:.4f}"
            f"  latency={result.total_latency_sec:.2f}s  emit={'on' if emitter else 'off'}"
        )

    asyncio.run(_go())


@lab_app.command("stats")
def lab_stats(
    task_type: str = typer.Option("", "--task-type", help="过滤 task_type, 空 = 全部"),
    top_k: int = typer.Option(10, "--top", help="显示 top N strategy"),
) -> None:
    """显示 ExperimentLog 当前累积统计 (in-process singleton)."""
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    experiments = log.list_all() if not task_type else log.by_task_type(task_type)
    if not experiments:
        console.print(
            "[yellow]ExperimentLog empty[/] (单进程 singleton; 用 `kun lab run` 累积数据)"
        )
        return

    stats = log.recipe_stats(task_type or None)
    stats.sort(key=lambda s: s.win_rate, reverse=True)

    table = Table(title=f"recipe stats (n_experiments={len(experiments)})")
    table.add_column("task_type")
    table.add_column("strategy")
    table.add_column("wins/total", justify="right")
    table.add_column("win_rate", justify="right")
    table.add_column("avg_score", justify="right")
    table.add_column("avg_cost $", justify="right")
    for s in stats[:top_k]:
        table.add_row(
            s.task_type,
            s.strategy,
            f"{s.win_count}/{s.total_count}",
            f"{s.win_rate:.2f}",
            f"{s.avg_score:.2f}",
            f"{s.avg_cost_usd:.4f}",
        )
    console.print(table)
    console.print(f"[dim]total lab cost: ${log.total_lab_cost_usd():.4f}[/]")


def _lab_find_experiment(experiment_id: str) -> Any:
    from kun.lab import get_experiment_log

    for exp in get_experiment_log().list_all():
        if exp.experiment_id == experiment_id:
            return exp
    console.print(f"[bold red]experiment not found[/]: {experiment_id}")
    raise typer.Exit(code=2)


def _winner_score(result: Any) -> float:
    winner_idx = result.winning_path_idx
    for path in result.path_results:
        if path.path_idx == winner_idx:
            return float(path.score)
    return 0.0


@lab_app.command("inspect")
def lab_inspect(
    experiment_id: str = typer.Argument(..., help="Experiment id"),
) -> None:
    """查看一次 lab experiment 的完整路径输出."""
    exp = _lab_find_experiment(experiment_id)
    result = exp.ensemble_result

    table = Table(title=f"experiment {exp.experiment_id} — task_type={exp.task_type}")
    table.add_column("winner")
    table.add_column("idx", justify="right")
    table.add_column("strategy")
    table.add_column("tier")
    table.add_column("temp", justify="right")
    table.add_column("score", justify="right")
    table.add_column("cost $", justify="right")
    table.add_column("error")
    table.add_column("output")
    for path in result.path_results:
        table.add_row(
            "✓" if path.path_idx == result.winning_path_idx else "",
            str(path.path_idx),
            str(path.config.get("strategy", "")),
            str(path.config.get("tier", "")),
            str(path.config.get("temperature", "")),
            f"{path.score:.2f}",
            f"{path.cost_usd:.4f}",
            path.error,
            path.output,
        )
    console.print(table)
    console.print(
        f"[dim]selection={result.selection_reason} cost=${result.total_cost_usd:.4f} "
        f"latency={result.total_latency_sec:.2f}s budget_exceeded={result.budget_exceeded}[/]"
    )
    for path in result.path_results:
        console.print(f"[bold]path {path.path_idx} output[/]\n{path.output}")


@lab_app.command("explain")
def lab_explain(
    experiment_id: str = typer.Argument(..., help="Experiment id"),
) -> None:
    """用便宜 judge 模型解释为什么 winner 更好."""
    from kun.interface.llm import get_router
    from kun.interface.llm.base import LLMMessage, LLMRequest, TaskProfile

    exp = _lab_find_experiment(experiment_id)
    result = exp.ensemble_result
    prompt = str(result.config.metadata.get("prompt", ""))
    path_summary = [
        {
            "idx": p.path_idx,
            "strategy": p.config.get("strategy", ""),
            "score": p.score,
            "cost_usd": p.cost_usd,
            "error": p.error,
            "output": p.output[:1200],
            "winner": p.path_idx == result.winning_path_idx,
        }
        for p in result.path_results
    ]

    async def _go() -> None:
        router = get_router()
        req = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content="你是 KUN-Lab 实验解释员，用中文大白话解释 winner 为什么更好。",
                ),
                LLMMessage(
                    role="user",
                    content=(
                        f"用户原始 prompt:\n{prompt or '[旧实验未保存 prompt]'}\n\n"
                        f"实验路径 JSON:\n{json.dumps(path_summary, ensure_ascii=False)}\n\n"
                        "请输出一段 markdown，说明 winner 的优势、其他路径的问题、"
                        "以及下次可复用的 recipe。"
                    ),
                ),
            ],
            temperature=0.2,
            max_tokens=1200,
            profile=TaskProfile(task_type="kun_lab.explain", prefer_speed=True),
        )
        resp = await router.invoke(req, purpose="judge")
        console.print(resp.content)

    asyncio.run(_go())


@lab_app.command("replay")
def lab_replay(
    experiment_id: str = typer.Argument(..., help="Experiment id"),
    enable: bool = typer.Option(
        False,
        "--enable",
        help="自动 set KUN_LAB_MODE=1 (默认要求用户已 export)",
    ),
    no_emit: bool = typer.Option(False, "--no-emit", help="不 emit experiment.created 事件"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """用同 prompt + 同 EnsembleConfig 重跑一次 experiment."""
    import os

    if enable:
        os.environ["KUN_LAB_MODE"] = "1"

    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.lab import EnsembleExecutor, LabEventEmitter, get_experiment_log, make_default_adapter
    from kun.lab.ensemble_executor import is_lab_enabled

    if not is_lab_enabled():
        console.print("[bold red]KUN-Lab 未启用[/]: export KUN_LAB_MODE=1, 或加 --enable flag")
        raise typer.Exit(code=2)

    exp = _lab_find_experiment(experiment_id)
    prompt = str(exp.ensemble_result.config.metadata.get("prompt", "")).strip()
    if not prompt:
        console.print(
            "[bold red]无法 replay[/]: 这条旧实验没有保存原始 prompt。"
            "请用 C30 之后的 `kun lab run` 生成新实验。"
        )
        raise typer.Exit(code=2)

    async def _go() -> None:
        cfg = exp.ensemble_result.config.model_copy(deep=True)
        cfg.metadata = {**cfg.metadata, "prompt": prompt, "replay_of": experiment_id}
        adapter = make_default_adapter(task_type=exp.task_type)
        emitter = None if no_emit else LabEventEmitter(task_type_default=exp.task_type)
        executor = EnsembleExecutor(
            adapter,
            event_emitter=emitter.on_experiment_completed if emitter else None,
        )

        with tenant_scope(TenantContext(tenant_id=tenant)):
            new_result = await executor.run(prompt, config=cfg, task_type=exp.task_type)
            get_experiment_log().record(
                task_type=exp.task_type,
                ensemble_result=new_result,
                prompt_hash=exp.prompt_hash,
                notes=f"replay_of={experiment_id}",
            )

        old_score = _winner_score(exp.ensemble_result)
        new_score = _winner_score(new_result)
        table = Table(title=f"replay {experiment_id} → {new_result.experiment_id}")
        table.add_column("metric")
        table.add_column("old")
        table.add_column("new")
        table.add_row(
            "winner_idx",
            str(exp.ensemble_result.winning_path_idx),
            str(new_result.winning_path_idx),
        )
        table.add_row("winner_score", f"{old_score:.2f}", f"{new_score:.2f}")
        table.add_row(
            "cost_usd",
            f"{exp.ensemble_result.total_cost_usd:.4f}",
            f"{new_result.total_cost_usd:.4f}",
        )
        console.print(table)

    asyncio.run(_go())


@lab_app.command("promote")
def lab_promote(
    min_total: int = typer.Option(10, "--min-total", help="至少累计实验数"),
    min_winrate: float = typer.Option(0.6, "--min-winrate", min=0.0, max=1.0),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="dry_run 默认 (只列 eligible); --apply 真推 events bus",
    ),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
) -> None:
    """找符合 min_total + min_winrate 的 recipe → 推主仓库 (KnowledgePrecipitation)."""
    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.lab import LabEventEmitter, RecipePromoter, get_experiment_log

    log = get_experiment_log()
    if not log.list_all():
        console.print("[yellow]ExperimentLog empty[/] (单进程 singleton; 先 `kun lab run` 跑实验)")
        return

    async def _go() -> None:
        emitter = None if dry_run else LabEventEmitter()
        promoter = RecipePromoter(
            log,
            min_total=min_total,
            min_winrate=min_winrate,
            event_emitter=emitter.on_recipe_promoted if emitter else None,
        )
        eligible = promoter.find_eligible_recipes()

        table = Table(title=f"eligible recipes (min_total={min_total}, min_winrate={min_winrate})")
        table.add_column("task_type")
        table.add_column("strategy")
        table.add_column("wins/total", justify="right")
        table.add_column("win_rate", justify="right")
        for s in eligible:
            table.add_row(
                s.task_type,
                s.strategy,
                f"{s.win_count}/{s.total_count}",
                f"{s.win_rate:.2f}",
            )
        console.print(table)

        if dry_run:
            console.print(
                "[dim]dry_run — 不 emit. 加 --apply 才真推 (会发 experiment.promoted 事件).[/]"
            )
            return

        with tenant_scope(TenantContext(tenant_id=tenant)):
            promotions = await promoter.promote_eligible()
        console.print(
            f"[green]推升 {len(promotions)} 条 recipe → events bus[/] "
            f"(experiment.promoted; 主仓库 idle_batch 消费)"
        )

    asyncio.run(_go())


@lab_benchmark_app.command("suite")
def lab_benchmark_suite(
    dataset_name: str = typer.Argument(..., help="Dataset name, e.g. marketing_copy"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="只跑前 N 条，便于 smoke"),
    paths: int = typer.Option(5, "--paths", "-n", min=2, max=10),
    cost_budget: float = typer.Option(1.0, "--cost-budget", help="单题 lab 总预算 USD"),
    enable: bool = typer.Option(False, "--enable", help="自动 set KUN_LAB_MODE=1"),
) -> None:
    """Run a benchmark suite and print strategy win rates."""
    import os

    if enable:
        os.environ["KUN_LAB_MODE"] = "1"

    from kun.lab import (
        BenchmarkRunOptions,
        EnsembleExecutor,
        get_experiment_log,
        load_benchmark_dataset,
        make_default_adapter,
        run_benchmark_suite,
    )
    from kun.lab.ensemble_executor import is_lab_enabled

    if not is_lab_enabled():
        console.print("[bold red]KUN-Lab 未启用[/]: export KUN_LAB_MODE=1, 或加 --enable flag")
        raise typer.Exit(code=2)

    async def _go() -> None:
        dataset = load_benchmark_dataset(dataset_name)
        executor = EnsembleExecutor(make_default_adapter(task_type=f"lab_benchmark.{dataset.name}"))
        report = await run_benchmark_suite(
            dataset,
            executor=executor,
            experiment_log=get_experiment_log(),
            options=BenchmarkRunOptions(
                limit=limit,
                paths=paths,
                cost_budget_total_usd=cost_budget,
            ),
        )
        _print_benchmark_report(report)

    asyncio.run(_go())


@lab_app.command("cursor-truncate")
def lab_cursor_truncate(
    older_than_days: int = typer.Option(
        30,
        "--older-than-days",
        min=1,
        help="删除 updated_at 早于 N 天的 lab adoption cursor 行",
    ),
) -> None:
    """清理过期 lab adoption cursor 行.

    Cursor 是调度书签, 不是长期记忆. 默认删 30 天前的行, 避免运维表无限长.
    """
    from kun.lab import truncate_lab_adoption_cursors

    async def _go() -> None:
        deleted = await truncate_lab_adoption_cursors(older_than_days=older_than_days)
        console.print(f"[green]deleted[/] {deleted} stale lab adoption cursor rows")

    asyncio.run(_go())


@lab_benchmark_app.command("report")
def lab_benchmark_report(
    dataset: str = typer.Option(..., "--dataset", help="Dataset name, e.g. marketing_copy"),
) -> None:
    """Print in-process benchmark report from ExperimentLog."""
    from kun.lab import benchmark_report_from_log, get_experiment_log

    report = benchmark_report_from_log(get_experiment_log(), dataset)
    _print_benchmark_report(report)


@lab_benchmark_app.command("replay")
def lab_benchmark_replay(
    task_id: str = typer.Option(..., "--task-id", help="Historical task_id to replay"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    paths: int = typer.Option(5, "--paths", "-n", min=2, max=10),
    cost_budget: float = typer.Option(1.0, "--cost-budget", help="单题 lab 总预算 USD"),
    enable: bool = typer.Option(False, "--enable", help="自动 set KUN_LAB_MODE=1"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON report."),
) -> None:
    """Replay one historical task through lab ensemble and compare winner strategy."""
    import os

    if enable:
        os.environ["KUN_LAB_MODE"] = "1"

    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.lab import (
        BenchmarkRunOptions,
        EnsembleExecutor,
        get_experiment_log,
        make_default_adapter,
        replay_historical_task,
    )
    from kun.lab.ensemble_executor import is_lab_enabled

    if not is_lab_enabled():
        console.print("[bold red]KUN-Lab 未启用[/]: export KUN_LAB_MODE=1, 或加 --enable flag")
        raise typer.Exit(code=2)

    async def _go() -> None:
        with tenant_scope(TenantContext(tenant_id=tenant)):
            report = await replay_historical_task(
                task_id,
                tenant_id=tenant,
                executor=EnsembleExecutor(make_default_adapter(task_type="lab_replay")),
                experiment_log=get_experiment_log(),
                options=BenchmarkRunOptions(paths=paths, cost_budget_total_usd=cost_budget),
            )
        if json_output:
            console.print_json(report.model_dump_json())
            return
        _print_replay_report(report)

    asyncio.run(_go())


def _print_benchmark_report(report: Any) -> None:
    table = Table(title=f"lab benchmark — {report.dataset}")
    table.add_column("strategy")
    table.add_column("wins/total", justify="right")
    table.add_column("win_rate", justify="right")
    table.add_column("avg_score", justify="right")
    table.add_column("avg_cost $", justify="right")
    for stat in report.strategy_stats:
        table.add_row(
            stat.strategy,
            f"{stat.wins}/{stat.total}",
            f"{stat.win_rate:.2f}",
            f"{stat.avg_score:.2f}",
            f"{stat.avg_cost_usd:.4f}",
        )
    console.print(table)
    console.print(
        f"[dim]items={report.total_items} experiments={report.experiments} "
        f"total_cost=${report.total_cost_usd:.4f}[/]"
    )


def _print_replay_report(report: Any) -> None:
    table = Table(title=f"lab replay — {report.task_id}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("task_type", report.task_type)
    table.add_row("experiment_id", report.experiment_id)
    table.add_row("original_winner", report.original_winning_strategy or "-")
    table.add_row("replay_winner", report.replay_winning_strategy or "-")
    table.add_row(
        "match",
        "unknown"
        if report.matches_original is None
        else ("yes" if report.matches_original else "no"),
    )
    table.add_row("cost $", f"{report.total_cost_usd:.4f}")
    console.print(table)
    if report.winning_output_preview:
        console.print(f"[bold green]winner preview[/]: {report.winning_output_preview!r}")


@lab_app.command("dogfood")
def lab_dogfood(
    task_types: str = typer.Option(
        "writing.creative,decision.product,coding.refactor,analysis.market,creative.ideation",
        "--types",
        help="逗号分隔的 task_type 列表 (默认 5 种典型: 写作/决策/编程/分析/创意)",
    ),
    paths: int = typer.Option(3, "--paths", "-n", min=2, max=10),
    enable: bool = typer.Option(False, "--enable", help="自动 set KUN_LAB_MODE=1"),
    tenant: str = typer.Option("u-sylvan", "--tenant"),
    report_path: Path | None = typer.Option(
        None,
        "--report-path",
        help="把 dogfood 汇总写成 JSON 文件，便于发给 PM/team 审核",
    ),
) -> None:
    """跑 V2.2 §26 完整闭环 dogfood — 显示全链路 trace.

    流程:
      1. 跑 N 种 task 的 ensemble (mock invoker, 不烧真 LLM)
      2. promote 推 recipe (低门槛让 recipe 真出来)
      3. 显示 LabRecipeRegistry 状态
      4. 显示 ExecutionMode classifier 对每个 task_type 的决策 (有 lab hint)

    示例:
        kun lab dogfood --enable --paths 3
    """
    import os

    if enable:
        os.environ["KUN_LAB_MODE"] = "1"

    from kun.api.execution_mode_classifier import classify_execution_mode
    from kun.core.tenancy import TenantContext, tenant_scope
    from kun.datamodel.soul_file import SoulFile
    from kun.lab import (
        EnsembleConfig,
        EnsembleExecutor,
        LabRecipeEntry,
        RecipePromoter,
        get_experiment_log,
        get_recipe_registry,
        reset_recipe_registry,
    )
    from kun.lab.ensemble_executor import is_lab_enabled

    if not is_lab_enabled():
        console.print("[bold red]KUN-Lab 未启用[/]: 加 --enable flag")
        raise typer.Exit(code=2)

    types = [t.strip() for t in task_types.split(",") if t.strip()]
    if not types:
        console.print("[red]需要至少 1 个 task_type[/]")
        raise typer.Exit(code=2)

    reset_recipe_registry()

    async def _go() -> None:
        # 用 mock invoker — dogfood 不烧真 LLM
        async def mock_invoker(prompt: str, path: Any) -> tuple[str, float, float]:
            import asyncio
            import random

            await asyncio.sleep(0.01)
            # 模拟不同 strategy 不同效果: tier_top 总赢
            quality = (
                0.9
                if path.tier == "top"
                else (0.7 if path.tier == "strong" else 0.4 + random.random() * 0.3)
            )
            output = f"[{path.strategy}] 模拟回答 quality={quality:.2f}"
            return (output, 0.01, 0.01)

        async def mock_scorer(output: str, prompt: str) -> float:
            # 摘 [strategy] 后的 quality 数字
            import re

            m = re.search(r"quality=([\d.]+)", output)
            return float(m.group(1)) if m else 0.5

        log = get_experiment_log()
        registry = get_recipe_registry()

        with tenant_scope(TenantContext(tenant_id=tenant)):
            # ---- Step 1: 跑各 task_type 多次 ----
            console.print("\n[bold cyan]═══ Step 1/4: 跑 ensemble dogfood ═══[/]\n")
            for task_type in types:
                executor = EnsembleExecutor(mock_invoker)
                # 跑 5 次让 promote 有数据
                for i in range(5):
                    result = await executor.run(
                        f"dogfood task {i} for {task_type}",
                        config=EnsembleConfig(n_paths=paths, selection_method="best_score"),
                        scoring_fn=mock_scorer,
                        task_type=task_type,
                    )
                    log.record(task_type=task_type, ensemble_result=result)
                console.print(f"  [green]✓[/] {task_type}: 跑 5 次 ensemble × {paths} path")

            # ---- Step 2: lab stats ----
            console.print("\n[bold cyan]═══ Step 2/4: ExperimentLog 累积 ═══[/]\n")
            stats = log.recipe_stats(task_type=None)
            stats.sort(key=lambda s: s.win_rate, reverse=True)
            stats_table = Table(title="recipe_stats")
            stats_table.add_column("task_type")
            stats_table.add_column("strategy")
            stats_table.add_column("wins/total", justify="right")
            stats_table.add_column("win_rate", justify="right")
            for s in stats[:8]:
                stats_table.add_row(
                    s.task_type,
                    s.strategy[:18],
                    f"{s.win_count}/{s.total_count}",
                    f"{s.win_rate:.2f}",
                )
            console.print(stats_table)

            # ---- Step 3: promote → registry (绕 events bus 直接 upsert) ----
            console.print("\n[bold cyan]═══ Step 3/4: Promote 推 recipe → registry ═══[/]\n")
            promoter = RecipePromoter(log, min_total=2, min_winrate=0.4)
            eligible = promoter.find_eligible_recipes()
            for s in eligible:
                # 直接 upsert 到 registry (跳过 events bus, dogfood 简化)
                target = (
                    "execution_mode_classifier"
                    if "tier_" in s.strategy
                    else "hermes_prompt_template"
                )
                registry.upsert(
                    LabRecipeEntry(
                        task_type=s.task_type,
                        target_module=target,
                        strategy=s.strategy,
                        win_rate=s.win_rate,
                        confidence=min(1.0, s.win_rate),
                    )
                )
                console.print(
                    f"  [green]✓[/] {s.task_type} → {s.strategy} ({s.win_rate:.0%}) → registry"
                )
            console.print(f"\n  registry size now: [bold]{len(registry)}[/]")

            # ---- Step 4: classifier 看 lab hint 真生效 ----
            console.print("\n[bold cyan]═══ Step 4/4: ExecutionMode classifier 决策 ═══[/]\n")
            soul = SoulFile(
                user_id="dogfood",
                approval_threshold_money=10.0,
                execution_mode_preference={"default_mode": "FAST"},
            )
            decision_table = Table(title="classifier 决策 (default=FAST)")
            decision_table.add_column("task_type")
            decision_table.add_column("mode", justify="center")
            decision_table.add_column("reason")
            classifier_decisions: list[dict[str, Any]] = []
            for t in types:
                mode, reason = classify_execution_mode({"task_type": t}, soul)
                classifier_decisions.append({"task_type": t, "mode": mode, "reason": reason})
                decision_table.add_row(t, f"[bold]{mode}[/]", reason[:60])
            console.print(decision_table)
            console.print("\n[dim]如 reason 含 'lab_recipe:tier_xxx' → V2.2 §26 闭环真生效 ✓[/]")
            console.print("[dim]如 mode=MAX 但 default=FAST → lab 推荐覆盖了 default[/]")

        if report_path is not None:
            report = {
                "tenant": tenant,
                "task_types": types,
                "paths": paths,
                "experiment_count": len(log.list_all()),
                "total_lab_cost_usd": log.total_lab_cost_usd(),
                "top_recipes": [
                    {
                        "task_type": s.task_type,
                        "strategy": s.strategy,
                        "win_count": s.win_count,
                        "total_count": s.total_count,
                        "win_rate": s.win_rate,
                        "avg_score": s.avg_score,
                        "avg_cost_usd": s.avg_cost_usd,
                    }
                    for s in stats[:20]
                ],
                "registry": [
                    {
                        "task_type": entry.task_type,
                        "target_module": entry.target_module,
                        "strategy": entry.strategy,
                        "win_rate": entry.win_rate,
                        "confidence": entry.confidence,
                    }
                    for entry in registry.all()
                ],
                "classifier_decisions": classifier_decisions,
            }
            await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                report_path.write_text,
                json.dumps(report, ensure_ascii=False, indent=2),
            )
            console.print(f"\n[green]dogfood report written[/]: {report_path}")

        console.print("\n[bold green]═══ Dogfood 完成 ═══[/]")
        console.print(
            "[dim]这是 in-process dogfood (跳 events bus + DB). 真闭环验证看 ./scripts/dogfood_run.sh[/]"
        )

    asyncio.run(_go())


if __name__ == "__main__":
    app()
