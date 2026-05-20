"""External Frontier50 runner adapter for the V6 Control Plane.

The pure Qi contract in ``qi_ab.py`` decides whether a completed round passes.
This module owns the boundary to the existing AB workspace: run one external
round command, read the produced files, and normalize them into V6 objects.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.nuo import (
    NuoObservation,
    build_nuo_recovery_plan,
    diagnose_nuo_health,
)
from kun.control_plane.qi_ab import (
    QI_AB_EXPECTED_REVIEW_COUNT,
    QiABRoundSummary,
    build_qi_ab_round_contract,
)
from kun.control_plane.runtime import WorkItemResult
from kun.control_plane.v6 import WorkItem

FRONTIER50_DEFAULT_WORKDIR = Path("/Users/slyvan/Documents/Codex/2026-05-08/5-7-1-2-abtext-10")
FRONTIER50_DEFAULT_COMMAND = "run_real_comparator_ab_external.command"


class ExternalCommandResult(BaseModel):
    """Normalized result from an external command invocation."""

    model_config = ConfigDict(extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = Field(default=0.0, ge=0.0)


class Frontier50ExternalRoundConfig(BaseModel):
    """Configuration for one external Frontier50 round run."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    workdir: Path = FRONTIER50_DEFAULT_WORKDIR
    command: Path | None = None
    round_index: int = Field(ge=1, le=10)
    round_id: str
    run_tag: str
    timeout_sec: int = Field(default=900, ge=1)
    review_timeout_sec: int = Field(default=600, ge=1)
    command_timeout_sec: int | None = Field(default=None, ge=1)
    max_concurrency: int = Field(default=2, ge=1)
    hermes_max_turns: int = Field(default=3, ge=1)
    agents: str = "kun,openclaw,hermes,llm_direct"
    review_agents: str = "openclaw,hermes,llm_direct"
    score_mode: str = "effect-first"

    @property
    def command_path(self) -> Path:
        return self.command or self.workdir / FRONTIER50_DEFAULT_COMMAND

    @property
    def round_dir(self) -> Path:
        return (
            self.workdir
            / "fair_ab_outputs"
            / "real_comparator_ab_external_live"
            / "groups"
            / f"group-{self.round_index:02d}"
            / self.run_tag
        )


CommandExecutor = Callable[[Path, Mapping[str, str], Path, int], ExternalCommandResult]


class Frontier50ExternalRoundRunner:
    """ControlPlaneRunner adapter around the existing Frontier50 command."""

    runner_type = "command"
    runner_identity = "qi-frontier50-external-runner"

    def __init__(
        self,
        *,
        config: Frontier50ExternalRoundConfig,
        mission_id: str,
        task_plan_version: str,
        task_ids: list[str],
        next_round_id: str | None = None,
        next_round_task_ids: list[str] | None = None,
        executor: CommandExecutor | None = None,
    ) -> None:
        self.config = config
        self.mission_id = mission_id
        self.task_plan_version = task_plan_version
        self.task_ids = task_ids
        self.next_round_id = next_round_id
        self.next_round_task_ids = next_round_task_ids or []
        self._executor = executor or _subprocess_executor

    def can_run(self, work_item: WorkItem) -> bool:
        text = f"{work_item.work_item_id}\n{work_item.expected_output}".lower()
        return work_item.owner == "qi" and work_item.type == "test" and (
            "frontier50" in text or "ab" in text
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        env = self._env()
        result = self._executor(
            self.config.command_path,
            env,
            self.config.workdir,
            self.config.command_timeout_sec or self.config.timeout_sec,
        )
        if self.config.round_dir.exists():
            try:
                summary = load_frontier50_round_summary(
                    self.config.round_dir,
                    mission_id=self.mission_id,
                    task_plan_version=self.task_plan_version,
                    round_id=self.config.round_id,
                    work_item_id=work_item.work_item_id,
                    task_ids=self.task_ids,
                )
                return build_qi_ab_round_contract(
                    summary,
                    next_round_id=self.next_round_id,
                    next_round_task_ids=self.next_round_task_ids,
                ).work_item_result
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return _blocked_result_from_error(
                    mission_id=self.mission_id,
                    task_plan_version=self.task_plan_version,
                    subject_ref=work_item.work_item_id,
                    error_text=f"{result.stderr}\n{exc}",
                    output_text=result.stdout,
                )
        return _blocked_result_from_error(
            mission_id=self.mission_id,
            task_plan_version=self.task_plan_version,
            subject_ref=work_item.work_item_id,
            error_text=result.stderr,
            output_text=result.stdout,
        )

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "RUN_GROUPS": str(self.config.round_index),
                "RUN_TAG": self.config.run_tag,
                "SUITE_PROFILE": "frontier-50",
                "SUITE_SIZE": "50",
                "GROUP_SIZE": "5",
                "AGENTS": self.config.agents,
                "REVIEW_AGENTS": self.config.review_agents,
                "TIMEOUT_SEC": str(self.config.timeout_sec),
                "REVIEW_TIMEOUT_SEC": str(self.config.review_timeout_sec),
                "HERMES_MAX_TURNS": str(self.config.hermes_max_turns),
                "MAX_CONCURRENCY": str(self.config.max_concurrency),
                "SCORE_MODE": self.config.score_mode,
                "KUN_AB_DISABLE_EXTERNAL_COMPAT_FALLBACK": "1",
                "KUN_AB_FALLBACK_ON_NATIVE_TIMEOUT": "0",
            }
        )
        return env


class Frontier50ExternalRuntimeRunner:
    """Daemon-ready Frontier50 runner that builds one round config per work item."""

    runner_type = "command"
    runner_identity = "qi-frontier50-external-runtime-runner"

    def __init__(
        self,
        *,
        workdir: Path = FRONTIER50_DEFAULT_WORKDIR,
        command: Path | None = None,
        run_tag: str | None = None,
        timeout_sec: int = 900,
        review_timeout_sec: int = 600,
        command_timeout_sec: int | None = None,
        task_ids: Sequence[str] = (),
        next_round_task_ids: Sequence[str] = (),
        executor: CommandExecutor | None = None,
    ) -> None:
        self.workdir = workdir
        self.command = command
        self.run_tag = run_tag
        self.timeout_sec = timeout_sec
        self.review_timeout_sec = review_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.task_ids = list(task_ids)
        self.next_round_task_ids = list(next_round_task_ids)
        self._executor = executor

    def can_run(self, work_item: WorkItem) -> bool:
        text = f"{work_item.work_item_id}\n{work_item.expected_output}".lower()
        return work_item.owner == "qi" and work_item.type == "test" and (
            "frontier50" in text or "ab" in text
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        round_index = _round_index_from_work_item(work_item)
        round_id = _round_id(round_index)
        runner = Frontier50ExternalRoundRunner(
            config=Frontier50ExternalRoundConfig(
                workdir=self.workdir,
                command=self.command,
                round_index=round_index,
                round_id=round_id,
                run_tag=self.run_tag or f"frontier50-r{round_index:02d}-control-plane-live",
                timeout_sec=self.timeout_sec,
                review_timeout_sec=self.review_timeout_sec,
                command_timeout_sec=self.command_timeout_sec,
            ),
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            task_ids=self.task_ids or _default_task_ids(round_index),
            next_round_id=_round_id(round_index + 1) if round_index < 10 else None,
            next_round_task_ids=list(self.next_round_task_ids)
            or (_default_task_ids(round_index + 1) if round_index < 10 else []),
            executor=self._executor,
        )
        return runner.run(work_item)


def load_frontier50_round_summary(
    round_dir: Path,
    *,
    mission_id: str,
    task_plan_version: str,
    round_id: str,
    work_item_id: str,
    task_ids: list[str] | None = None,
) -> QiABRoundSummary:
    """Read existing Frontier50 round artifacts into a QiABRoundSummary."""

    report_path = round_dir / "report.json"
    health_path = round_dir / "comparator_health.json"
    repair_path = round_dir / "repair_tickets.json"
    runs_path = round_dir / "runs.jsonl"
    reviews_path = round_dir / "reviews.jsonl"
    if not report_path.exists():
        raise ValueError(f"report missing: {report_path}")
    report = _load_json(report_path)
    health = _load_json(health_path) if health_path.exists() else {}
    repairs = _load_json_list(repair_path) if repair_path.exists() else []
    kun = _ranking_by_agent(report).get("kun", {})
    kun_gate_passed = _kun_gate_passed(report=report, health=health)
    actionable_repairs = [
        item for item in repairs if not (kun_gate_passed and item.get("review_only") is True)
    ]
    review_only_count = len(repairs) - len(actionable_repairs)
    return QiABRoundSummary(
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        round_id=round_id,
        work_item_id=work_item_id,
        task_ids=task_ids or _task_ids_from_report(report),
        answer_refs=_jsonl_refs(runs_path),
        review_refs=_jsonl_refs(reviews_path),
        report_ref=_artifact_ref(report_path),
        health_ref=_artifact_ref(health_path) if health_path.exists() else None,
        repair_ticket_refs=_repair_refs(repair_path, actionable_repairs),
        comparator_healthy=not bool(health.get("comparator_unhealthy")),
        kun_gate_passed=kun_gate_passed,
        kun_result_quality=float(kun.get("avg_overall_score") or 0.0),
        speed=float(kun.get("avg_speed_score") or 0.0),
        cost=float(kun.get("avg_cost_score") or 0.0),
        notes=[f"ignored {review_only_count} review-only learning ticket(s) on a passing round"]
        if review_only_count
        else [],
    )


def _subprocess_executor(
    command: Path,
    env: Mapping[str, str],
    cwd: Path,
    timeout_sec: int,
) -> ExternalCommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [_command_shell(), str(command)],
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        return ExternalCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_sec=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return ExternalCommandResult(
            exit_code=124,
            stdout=_text(exc.stdout),
            stderr=f"timeout after {timeout_sec}s\n{_text(exc.stderr)}",
            duration_sec=time.monotonic() - started,
        )


def _command_shell() -> str:
    return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


def _blocked_result_from_error(
    *,
    mission_id: str,
    task_plan_version: str,
    subject_ref: str,
    error_text: str,
    output_text: str,
) -> WorkItemResult:
    environment_blocked = _looks_environment_blocked(f"{error_text}\n{output_text}")
    report = diagnose_nuo_health(
        NuoObservation(
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            subject_ref=subject_ref,
            task_type="self_improvement",
            output_text=output_text,
            error_text=error_text,
            report_required=not environment_blocked,
            expected_review_count=0 if environment_blocked else QI_AB_EXPECTED_REVIEW_COUNT,
        )
    )
    plan = build_nuo_recovery_plan(report, depends_on_subject=False)
    gate = plan.gate_evaluation
    followup_work_items = [plan.recovery_work_item] if plan.recovery_work_item else []
    return WorkItemResult(
        status="failed",
        summary="Frontier50 external round blocked before trustworthy scoring.",
        gate_evaluation=gate,
        failure_category=gate.failure_category,
        followup_work_items=followup_work_items,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected JSON list: {path}")
    return [item for item in payload if isinstance(item, dict)]


def _ranking_by_agent(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("agent_ref") or ""): row
        for row in report.get("rankings", [])
        if isinstance(row, dict)
    }


def _task_ids_from_report(report: dict[str, Any]) -> list[str]:
    task_scores = report.get("task_scores")
    if not isinstance(task_scores, list):
        return []
    return [
        str(row.get("task_id"))
        for row in task_scores
        if isinstance(row, dict) and row.get("task_id")
    ]


def _jsonl_refs(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        f"{_artifact_ref(path)}#L{index}"
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip()
    ]


def _repair_refs(path: Path, repairs: list[dict[str, Any]]) -> list[str]:
    if not path.exists():
        return []
    if not repairs:
        return []
    return [
        f"{_artifact_ref(path)}#{item.get('ticket_id') or index}"
        for index, item in enumerate(repairs, start=1)
    ]


def _artifact_ref(path: Path) -> str:
    return str(path)


def _looks_environment_blocked(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "network eof",
            "unexpected eof",
            "wrapper missing",
            "wrapper not found",
            "auth failure",
            "authentication failed",
        )
    )


def _kun_gate_passed(*, report: dict[str, Any], health: dict[str, Any]) -> bool:
    if health.get("comparator_unhealthy"):
        return False
    rankings = report.get("rankings") or []
    if not rankings or not isinstance(rankings[0], dict):
        return False
    kun = _ranking_by_agent(report).get("kun", {})
    if rankings[0].get("agent_ref") != "kun":
        return False
    if float(kun.get("avg_overall_score") or 0.0) < 0.90:
        return False
    if float(kun.get("avg_effect_score") or 0.0) < 0.88:
        return False
    if float(kun.get("avg_engineering_score") or 0.0) < 0.90:
        return False
    return not any(
        isinstance(gap, dict)
        and (gap.get("severity") in {"error", "critical"} or float(gap.get("delta") or 0.0) >= 0.05)
        for gap in report.get("gaps", [])
    )


def _round_index_from_work_item(work_item: WorkItem) -> int:
    text = f"{work_item.work_item_id}\n{work_item.expected_output}".lower()
    for value in range(1, 11):
        if f"round-{value:02d}" in text or f"round-{value}" in text:
            return value
        if f"r{value:02d}" in text:
            return value
    return 1


def _round_id(round_index: int) -> str:
    return f"round-{round_index:02d}"


def _default_task_ids(round_index: int) -> list[str]:
    start = ((round_index - 1) * 5) + 1
    return [f"task-{index}" for index in range(start, start + 5)]


__all__ = [
    "ExternalCommandResult",
    "Frontier50ExternalRoundConfig",
    "Frontier50ExternalRoundRunner",
    "Frontier50ExternalRuntimeRunner",
    "load_frontier50_round_summary",
]
