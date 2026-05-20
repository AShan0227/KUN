from __future__ import annotations

import pytest
from kun.skills.builtin import python_exec, shell_exec
from kun.skills.sandbox import resolve_execution_cwd

pytestmark = pytest.mark.unit


def test_resolve_execution_cwd_rejects_paths_outside_configured_roots(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "skill-root"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("KUN_SKILL_EXEC_ROOTS", str(root))

    assert resolve_execution_cwd(None) == root.resolve()
    assert resolve_execution_cwd("child") == (root / "child").resolve()

    with pytest.raises(ValueError, match="escapes executable skill sandbox"):
        resolve_execution_cwd(outside)


@pytest.mark.asyncio
async def test_python_exec_runs_inside_configured_sandbox(tmp_path, monkeypatch) -> None:
    root = tmp_path / "py-root"
    monkeypatch.setenv("KUN_SKILL_EXEC_ROOTS", str(root))

    result = await python_exec.execute(
        {
            "code": "import os; print(os.getcwd())",
            "timeout_sec": 5,
        }
    )

    assert result.ok is True
    assert result.output["stdout"].strip() == str(root.resolve())
    assert result.metadata["sandbox_enforced"] is True
    assert result.metadata["cwd"] == str(root.resolve())


@pytest.mark.asyncio
async def test_shell_exec_rejects_host_cwd_outside_sandbox(tmp_path, monkeypatch) -> None:
    root = tmp_path / "shell-root"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("KUN_SKILL_EXEC_ROOTS", str(root))

    result = await shell_exec.execute(
        {
            "command": "pwd",
            "cwd": str(outside),
            "timeout_sec": 5,
        }
    )

    assert result.ok is False
    assert "escapes executable skill sandbox" in result.error
