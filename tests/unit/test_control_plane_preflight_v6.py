from __future__ import annotations

from pathlib import Path

from kun.control_plane import (
    ExecutionContract,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)
from kun.control_plane.preflight import _planned_skill_runs


def test_preflight_uses_explicit_pytest_target_from_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "rainflow"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("", encoding="utf-8")

    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rainflow-seedance-stage1-real-v1",
        owner="kun",
        objective="Run RainFlow Seedance regression tests.",
        task_type="product_development",
        status="contracted",
        current_plan_version="v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-seedance",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["Run only the targeted Seedance bridge regression."],
        constraints=["Do not expand focused test work into the whole repository suite."],
        test_plan=["uv run pytest -q tests/engines/test_bridge.py -k seedance"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-seedance",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-seedance",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="operator",
        scope="seedance",
        summary="Seedance preflight command test.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-msn-rainflow-seedance-stage1-real-v1-02-seedance-regression-tests",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        workspace_ref=f"workspace://{workspace}",
        expected_output="Run targeted Seedance bridge tests.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    runs = _planned_skill_runs(control_plane=control_plane, work_item=work)

    pytest_runs = [
        params["command"]
        for skill_id, params in runs
        if skill_id == "shell-exec" and "pytest" in params["command"]
    ]
    assert pytest_runs == [".venv/bin/python -m pytest -q tests/engines/test_bridge.py -k seedance"]


def test_preflight_does_not_expand_retest_into_full_pytest_without_explicit_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "rainflow"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("", encoding="utf-8")

    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rainflow-retest",
        owner="kun",
        objective="Run clean RainFlow retest evidence.",
        task_type="product_development",
        status="contracted",
        current_plan_version="v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-retest",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["Clean retest evidence must pass."],
        test_plan=["Run clean retest evidence and report failures."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="operator",
        scope="rainflow",
        summary="Retest preflight should not run the full suite.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not run implicit full pytest during preflight."],
    )
    work = WorkItem(
        work_item_id="work-rainflow-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        workspace_ref=f"workspace://{workspace}",
        expected_output="Run clean retest evidence, quality gate, and regression checks.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    runs = _planned_skill_runs(control_plane=control_plane, work_item=work)

    assert not any(
        skill_id == "shell-exec" and "pytest" in params["command"] for skill_id, params in runs
    )
