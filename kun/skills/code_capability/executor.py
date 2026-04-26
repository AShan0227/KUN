"""CodeExecutor — run code, tests, and lint inside a bounded workspace."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_OUTPUT_LIMIT = 128 * 1024

LintTool = Literal["ruff", "black", "mypy"]


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    command: list[str]
    cwd: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    duration_sec: float = 0.0
    truncated: bool = False
    sandbox: dict[str, str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class TestResult:
    ok: bool
    passed: int
    failed: int
    skipped: int
    output: str
    error: str
    returncode: int | None
    duration_sec: float
    timed_out: bool = False


@dataclass(frozen=True)
class LintIssue:
    path: str
    line: int | None
    column: int | None
    message: str


@dataclass(frozen=True)
class LintResult:
    ok: bool
    tool: LintTool
    issues: list[LintIssue]
    output: str
    error: str
    returncode: int | None
    duration_sec: float
    timed_out: bool = False


class CodeExecutor:
    """Execute code with timeout and cwd restrictions.

    This is a soft sandbox today: it avoids shell interpolation, pins cwd under
    a task workspace, strips noisy telemetry env vars, and enforces timeout.
    C14 will replace/strengthen the backend with OS-level isolation.
    """

    def __init__(self, *, workspace_root: str | Path = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()

    async def execute_python(
        self,
        code: str,
        *,
        timeout_sec: int = 30,
        cwd: Path | None = None,
    ) -> ExecutionResult:
        """Run a Python snippet."""
        if not code.strip():
            return ExecutionResult(
                ok=False,
                command=[sys.executable, "-I", "-c", ""],
                cwd=str(self.workspace_root),
                stderr="code is required",
                returncode=2,
                sandbox=self._sandbox_meta(),
            )
        run_cwd = self._resolve_cwd(cwd)
        return await self._run(
            [sys.executable, "-I", "-c", code],
            cwd=run_cwd,
            timeout_sec=timeout_sec,
        )

    async def execute_test(
        self,
        test_path: str,
        *,
        timeout_sec: int = 60,
    ) -> TestResult:
        """Run pytest on a file or directory under the workspace root."""
        target = self._resolve_path(test_path)
        result = await self._run(
            [sys.executable, "-m", "pytest", str(target), "-q"],
            cwd=self.workspace_root,
            timeout_sec=timeout_sec,
        )
        combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
        passed, failed, skipped = _parse_pytest_counts(combined)
        return TestResult(
            ok=result.ok,
            passed=passed,
            failed=failed,
            skipped=skipped,
            output=result.stdout,
            error=result.stderr,
            returncode=result.returncode,
            duration_sec=result.duration_sec,
            timed_out=result.timed_out,
        )

    async def execute_lint(
        self,
        target: Path,
        tool: LintTool = "ruff",
        *,
        timeout_sec: int = 60,
    ) -> LintResult:
        """Run ruff/black/mypy checks under the workspace root."""
        resolved = self._resolve_path(target)
        command = _lint_command(tool, resolved)
        result = await self._run(command, cwd=self.workspace_root, timeout_sec=timeout_sec)
        combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return LintResult(
            ok=result.ok,
            tool=tool,
            issues=_parse_lint_issues(combined),
            output=result.stdout,
            error=result.stderr,
            returncode=result.returncode,
            duration_sec=result.duration_sec,
            timed_out=result.timed_out,
        )

    def _resolve_cwd(self, cwd: Path | None) -> Path:
        if cwd is None:
            return self.workspace_root
        return self._resolve_path(cwd, must_exist=True, require_dir=True)

    def _resolve_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
        require_dir: bool = False,
    ) -> Path:
        candidate = Path(path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_root / candidate).resolve()
        )
        if self.workspace_root != resolved and self.workspace_root not in resolved.parents:
            raise ValueError(f"path escapes code workspace: {path}")
        if must_exist and not resolved.exists():
            raise ValueError(f"path does not exist under workspace: {path}")
        if require_dir and not resolved.is_dir():
            raise ValueError(f"path is not a directory under workspace: {path}")
        return resolved

    async def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_sec: int,
    ) -> ExecutionResult:
        timeout = max(1, min(300, int(timeout_sec)))
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=_sandbox_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecutionResult(
                ok=False,
                command=command,
                cwd=str(cwd),
                stderr=f"timed out after {timeout}s",
                returncode=proc.returncode,
                timed_out=True,
                duration_sec=time.perf_counter() - started,
                sandbox=self._sandbox_meta(),
            )

        stdout, stdout_truncated = _decode_limited(stdout_bytes)
        stderr, stderr_truncated = _decode_limited(stderr_bytes)
        return ExecutionResult(
            ok=proc.returncode == 0,
            command=command,
            cwd=str(cwd),
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            duration_sec=time.perf_counter() - started,
            truncated=stdout_truncated or stderr_truncated,
            sandbox=self._sandbox_meta(),
        )

    def _sandbox_meta(self) -> dict[str, str | bool]:
        return {
            "kind": "soft",
            "cwd_restricted": True,
            "network_disabled": "best_effort",
            "workspace_root": str(self.workspace_root),
        }


def _sandbox_env() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "TERM",
        "TMPDIR",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "KUN_CODE_SANDBOX": "soft",
            "KUN_NETWORK_DISABLED": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return env


def _decode_limited(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", "replace")
    if len(text) <= _OUTPUT_LIMIT:
        return text, False
    return text[:_OUTPUT_LIMIT], True


def _lint_command(tool: LintTool, target: Path) -> list[str]:
    if tool == "ruff":
        return [sys.executable, "-m", "ruff", "check", str(target)]
    if tool == "black":
        return [sys.executable, "-m", "black", "--check", str(target)]
    return [sys.executable, "-m", "mypy", str(target)]


def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
    passed = _count_status(output, "passed")
    failed = _count_status(output, "failed")
    skipped = _count_status(output, "skipped")
    # `pytest -q` often prints a dot stream without a count for tiny all-pass runs.
    if passed == failed == skipped == 0 and "100%" in output and "failed" not in output.lower():
        passed = 1
    return passed, failed, skipped


def _count_status(output: str, status: str) -> int:
    match = re.search(rf"(\d+)\s+{status}", output)
    return int(match.group(1)) if match else 0


def _parse_lint_issues(output: str) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for line in output.splitlines():
        match = re.match(r"(?P<path>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)", line)
        if match is None:
            continue
        issues.append(
            LintIssue(
                path=match.group("path"),
                line=int(match.group("line")),
                column=int(match.group("col")),
                message=match.group("msg"),
            )
        )
    return issues


__all__ = [
    "CodeExecutor",
    "ExecutionResult",
    "LintIssue",
    "LintResult",
    "LintTool",
    "TestResult",
]
