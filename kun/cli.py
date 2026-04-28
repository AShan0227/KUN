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
    """List ProtocolRegistry entries for a tenant."""
    from kun.qi import get_protocol_registry

    async def _go() -> None:
        protocols = await get_protocol_registry().list_all(tenant)
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
    enabled = os.getenv("KUN_QI_ENABLED", "0") == "1"
    active = enabled and (forced or is_qi_window_active(cfg))
    budget = get_qi_budget()

    console.print(f"[bold cyan]启 (Qi) status[/] — tenant={tenant}")
    console.print(f"  KUN_QI_ENABLED      = {os.getenv('KUN_QI_ENABLED', '0')}")
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
