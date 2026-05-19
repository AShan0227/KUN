from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from kun.control_plane import WorkItem
from kun.control_plane.frontier50_external import (
    ExternalCommandResult,
    Frontier50ExternalRoundConfig,
    Frontier50ExternalRoundRunner,
    load_frontier50_round_summary,
)


def _round_dir(root: Path, tag: str = "frontier50-r01-test") -> Path:
    return (
        root
        / "fair_ab_outputs"
        / "real_comparator_ab_external_live"
        / "groups"
        / "group-01"
        / tag
    )


def _write_round(
    root: Path,
    *,
    tag: str = "frontier50-r01-test",
    comparator_healthy: bool = True,
    kun_best: bool = True,
    kun_overall: float = 0.93,
    kun_effect: float = 0.91,
    kun_engineering: float = 0.94,
    gaps: list[dict[str, object]] | None = None,
    repair_tickets: list[dict[str, object]] | None = None,
) -> Path:
    round_dir = _round_dir(root, tag)
    round_dir.mkdir(parents=True)
    rankings = [
        {
            "agent_ref": "kun" if kun_best else "hermes",
            "avg_overall_score": kun_overall if kun_best else 0.95,
            "avg_effect_score": kun_effect if kun_best else 0.94,
            "avg_speed_score": 1.0,
            "avg_cost_score": 1.0,
            "avg_evidence_score": 0.9,
            "avg_engineering_score": kun_engineering if kun_best else 0.95,
            "pass_rate": 1.0,
        },
        {
            "agent_ref": "hermes" if kun_best else "kun",
            "avg_overall_score": 0.88 if kun_best else kun_overall,
            "avg_effect_score": 0.87 if kun_best else kun_effect,
            "avg_speed_score": 1.0,
            "avg_cost_score": 1.0,
            "avg_evidence_score": 0.8,
            "avg_engineering_score": 0.87 if kun_best else kun_engineering,
            "pass_rate": 0.8,
        },
    ]
    (round_dir / "report.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "rankings": rankings,
                "task_scores": [{"task_id": f"task-{index}"} for index in range(1, 6)],
                "gaps": gaps or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (round_dir / "comparator_health.json").write_text(
        json.dumps({"comparator_unhealthy": not comparator_healthy}),
        encoding="utf-8",
    )
    (round_dir / "repair_tickets.json").write_text(
        json.dumps(repair_tickets or [], ensure_ascii=False),
        encoding="utf-8",
    )
    (round_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps({"run": index}) for index in range(20)) + "\n",
        encoding="utf-8",
    )
    (round_dir / "reviews.jsonl").write_text(
        "\n".join(json.dumps({"review": index}) for index in range(45)) + "\n",
        encoding="utf-8",
    )
    return round_dir


def _config(root: Path, tag: str = "frontier50-r01-test") -> Frontier50ExternalRoundConfig:
    return Frontier50ExternalRoundConfig(
        workdir=root,
        command=root / "run_real_comparator_ab_external.command",
        round_index=1,
        round_id="round-01",
        run_tag=tag,
    )


def test_load_frontier50_round_summary_reads_real_artifact_contract(tmp_path: Path) -> None:
    round_dir = _write_round(tmp_path)

    summary = load_frontier50_round_summary(
        round_dir,
        mission_id="msn-v6",
        task_plan_version="v1",
        round_id="round-01",
        work_item_id="work-qi-ab-round-01",
    )

    assert summary.answer_count == 20
    assert summary.review_count == 45
    assert summary.report_ref == str(round_dir / "report.json")
    assert summary.health_ref == str(round_dir / "comparator_health.json")
    assert summary.kun_gate_passed is True
    assert summary.kun_result_quality == 0.93


def test_passing_round_ignores_review_only_learning_tickets(tmp_path: Path) -> None:
    round_dir = _write_round(
        tmp_path,
        repair_tickets=[
            {
                "ticket_id": "review-only-gap",
                "review_only": True,
                "can_auto_apply": False,
            }
        ],
    )

    summary = load_frontier50_round_summary(
        round_dir,
        mission_id="msn-v6",
        task_plan_version="v1",
        round_id="round-01",
        work_item_id="work-qi-ab-round-01",
    )

    assert summary.kun_gate_passed is True
    assert summary.repair_ticket_refs == []
    assert "review-only" in summary.notes[0]


def test_external_round_runner_normalizes_polluted_round_without_agent_failure(
    tmp_path: Path,
) -> None:
    _write_round(tmp_path, comparator_healthy=False)
    captured: dict[str, object] = {}

    def fake_executor(
        command: Path,
        env: Mapping[str, str],
        cwd: Path,
        timeout_sec: int,
    ) -> ExternalCommandResult:
        captured.update({"command": command, "env": env, "cwd": cwd, "timeout_sec": timeout_sec})
        return ExternalCommandResult(exit_code=11, stdout="comparator unhealthy")

    runner = Frontier50ExternalRoundRunner(
        config=_config(tmp_path),
        mission_id="msn-v6",
        task_plan_version="v1",
        task_ids=[f"task-{index}" for index in range(1, 6)],
        next_round_id="round-02",
        next_round_task_ids=["task-6"],
        executor=fake_executor,
    )
    result = runner.run(
        WorkItem(
            work_item_id="work-qi-ab-round-01",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="test",
            owner="qi",
        )
    )

    assert captured["cwd"] == tmp_path
    captured_env = cast(Mapping[str, str], captured["env"])
    assert captured_env["RUN_GROUPS"] == "1"
    assert result.failure_category == "environment_failure"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.score_breakdown["agent_failure_counted"] == 0.0
    assert result.followup_work_items[0].owner == "nuo"
    assert all(item.work_item_id != "work-qi-ab-round-02" for item in result.followup_work_items)


def test_external_round_runner_separates_command_timeout_from_task_timeout(
    tmp_path: Path,
) -> None:
    _write_round(tmp_path)
    captured: dict[str, object] = {}

    def fake_executor(
        command: Path,
        env: Mapping[str, str],
        cwd: Path,
        timeout_sec: int,
    ) -> ExternalCommandResult:
        captured.update({"env": env, "timeout_sec": timeout_sec})
        return ExternalCommandResult(exit_code=0, stdout="ok")

    runner = Frontier50ExternalRoundRunner(
        config=Frontier50ExternalRoundConfig(
            workdir=tmp_path,
            command=tmp_path / "run_real_comparator_ab_external.command",
            round_index=1,
            round_id="round-01",
            run_tag="frontier50-r01-test",
            timeout_sec=900,
            command_timeout_sec=7200,
        ),
        mission_id="msn-v6",
        task_plan_version="v1",
        task_ids=[f"task-{index}" for index in range(1, 6)],
        executor=fake_executor,
    )

    result = runner.run(
        WorkItem(
            work_item_id="work-qi-ab-round-01",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="test",
            owner="qi",
        )
    )

    captured_env = cast(Mapping[str, str], captured["env"])
    assert captured["timeout_sec"] == 7200
    assert captured_env["TIMEOUT_SEC"] == "900"
    assert result.status == "done"


def test_external_round_runner_sends_network_eof_to_nuo_repair(tmp_path: Path) -> None:
    def fake_executor(
        _command: Path,
        _env: Mapping[str, str],
        _cwd: Path,
        _timeout_sec: int,
    ) -> ExternalCommandResult:
        return ExternalCommandResult(exit_code=1, stderr="network EOF while calling comparator")

    runner = Frontier50ExternalRoundRunner(
        config=_config(tmp_path),
        mission_id="msn-v6",
        task_plan_version="v1",
        task_ids=[f"task-{index}" for index in range(1, 6)],
        executor=fake_executor,
    )

    result = runner.run(
        WorkItem(
            work_item_id="work-qi-ab-round-01",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="test",
            owner="qi",
        )
    )

    assert result.status == "failed"
    assert result.failure_category == "environment_failure"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.responsibility_scope == "environment"
    assert result.gate_evaluation.next_action == "needs_repair"
    assert result.followup_work_items
    followup = result.followup_work_items[0]
    assert followup.owner == "control-plane"
    assert followup.type == "repair"
    assert followup.dependencies == []
    assert "network_eof" in followup.expected_output


def test_external_round_runner_executes_real_command_and_reads_artifacts(
    tmp_path: Path,
) -> None:
    tag = "frontier50-real-subprocess"
    command = tmp_path / "run_real_comparator_ab_external.command"
    command.write_text(
        f"""set -e
round_dir="$PWD/fair_ab_outputs/real_comparator_ab_external_live/groups/group-01/{tag}"
mkdir -p "$round_dir"
cat > "$round_dir/report.json" <<'JSON'
{{
  "status": "ok",
  "rankings": [
    {{
      "agent_ref": "kun",
      "avg_overall_score": 0.93,
      "avg_effect_score": 0.91,
      "avg_speed_score": 1.0,
      "avg_cost_score": 1.0,
      "avg_evidence_score": 0.9,
      "avg_engineering_score": 0.94,
      "pass_rate": 1.0
    }},
    {{
      "agent_ref": "hermes",
      "avg_overall_score": 0.88,
      "avg_effect_score": 0.87,
      "avg_speed_score": 1.0,
      "avg_cost_score": 1.0,
      "avg_evidence_score": 0.8,
      "avg_engineering_score": 0.87,
      "pass_rate": 0.8
    }}
  ],
  "task_scores": [
    {{"task_id": "task-1"}},
    {{"task_id": "task-2"}},
    {{"task_id": "task-3"}},
    {{"task_id": "task-4"}},
    {{"task_id": "task-5"}}
  ],
  "gaps": []
}}
JSON
cat > "$round_dir/comparator_health.json" <<'JSON'
{{"comparator_unhealthy": false}}
JSON
cat > "$round_dir/repair_tickets.json" <<'JSON'
[]
JSON
: > "$round_dir/runs.jsonl"
for index in {{1..20}}; do
  print '{{"run":'${{index}}'}}' >> "$round_dir/runs.jsonl"
done
: > "$round_dir/reviews.jsonl"
for index in {{1..45}}; do
  print '{{"review":'${{index}}'}}' >> "$round_dir/reviews.jsonl"
done
""",
        encoding="utf-8",
    )
    runner = Frontier50ExternalRoundRunner(
        config=_config(tmp_path, tag=tag),
        mission_id="msn-v6",
        task_plan_version="v1",
        task_ids=[f"task-{index}" for index in range(1, 6)],
        next_round_id="round-02",
        next_round_task_ids=["task-6", "task-7", "task-8", "task-9", "task-10"],
    )

    result = runner.run(
        WorkItem(
            work_item_id="work-qi-ab-round-01",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="test",
            owner="qi",
        )
    )

    assert result.status == "done"
    assert result.artifact_manifest is not None
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.north_star_verdict == "pass"
    assert result.followup_work_items[0].work_item_id == "work-qi-ab-round-02"
