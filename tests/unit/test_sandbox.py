"""C14 ToolCallSandbox tests."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

import pytest
from kun.security import sandbox as sandbox_mod
from kun.security.sandbox import ToolCallSandbox, detect_isolation_backend


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sandbox_runs_builtin_python_exec(tmp_path: Path) -> None:
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)], cpu_limit_sec=5)

    result = await sandbox.run(
        "python-exec",
        {"code": "print('hello sandbox')"},
        agent_id="agent-1",
    )

    assert result.success is True
    assert "hello sandbox" in result.output
    assert Path(result.cwd).name == tmp_path.name
    assert result.agent_id == "agent-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sandbox_hides_non_whitelisted_sensitive_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-test")
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)], allowed_envs=["PATH"], cpu_limit_sec=5)

    result = await sandbox.run(
        "python-exec",
        {"code": "import os; print(os.getenv('OPENAI_API_KEY'))"},
        agent_id="agent-1",
    )

    assert result.success is True
    assert "sk-secret-test" not in result.output
    assert "None" in result.output


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sandbox_timeout_marks_result(tmp_path: Path) -> None:
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)], cpu_limit_sec=1)

    result = await sandbox.run(
        "python-exec",
        {"code": "import time; time.sleep(5)"},
        agent_id="agent-1",
    )

    assert result.success is False
    assert result.timed_out is True
    assert result.error is not None
    assert "timed out" in result.error


@pytest.mark.unit
def test_sandbox_rejects_missing_allowed_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allowed path"):
        ToolCallSandbox(allowed_paths=[str(tmp_path / "missing")])


@pytest.mark.unit
def test_detect_escape_flags_host_path(tmp_path: Path) -> None:
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)])

    violations = sandbox.detect_escape("read /Users/petrarain/.ssh/id_rsa")

    assert any(v.kind == "path_leak" for v in violations)


@pytest.mark.unit
def test_detect_escape_allows_configured_path(tmp_path: Path) -> None:
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)])
    inside = tmp_path / "work.txt"

    violations = sandbox.detect_escape(f"wrote {inside}")

    assert not any(v.kind == "path_leak" for v in violations)


@pytest.mark.unit
def test_detect_escape_flags_sensitive_env_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_PRIVATE_TOKEN", "token-value-123")
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)])

    violations = sandbox.detect_escape("oops token-value-123")

    assert any(v.kind == "env_leak" and v.severity == "critical" for v in violations)


@pytest.mark.unit
def test_detect_escape_flags_process_and_network_signals(tmp_path: Path) -> None:
    sandbox = ToolCallSandbox(allowed_paths=[str(tmp_path)])

    violations = sandbox.detect_escape("PPID=1 and curl https://example.com")

    assert any(v.kind == "process_leak" for v in violations)
    assert any(v.kind == "network_attempt" for v in violations)


@pytest.mark.unit
def test_detect_isolation_backend_soft_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert detect_isolation_backend() == "soft"


@pytest.mark.unit
def test_detect_isolation_backend_prefers_platform_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        sandbox_mod.shutil,
        "which",
        lambda name: "/usr/bin/firejail" if name == "firejail" else None,
    )

    assert detect_isolation_backend() == "firejail"
