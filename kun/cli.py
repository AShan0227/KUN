"""KUN CLI (typer)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from kun import __version__
from kun.core.config import settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
security_app = typer.Typer(add_completion=False, no_args_is_help=True)
lab_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="KUN-Lab 内测分区 (V2.2 §26)"
)
lab_benchmark_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="KUN-Lab benchmark suites"
)
console = Console()
app.add_typer(security_app, name="security")
app.add_typer(lab_app, name="lab")
lab_app.add_typer(lab_benchmark_app, name="benchmark")


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
        )

        with tenant_scope(TenantContext(tenant_id=tenant)):
            result = await executor.run(task, config=cfg, task_type=task_type)
            # 同时记进 ExperimentLog 让 promote 子命令能看
            get_experiment_log().record(task_type=task_type, ensemble_result=result)

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


if __name__ == "__main__":
    app()
