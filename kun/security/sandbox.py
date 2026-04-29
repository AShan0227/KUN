"""ToolCallSandbox — minimal anti-escape sandbox for tool calls.

C14 intentionally starts with a soft but enforceable boundary:
- no shell interpolation for the sandbox runner itself;
- cwd is pinned to an allowed workspace;
- env is whitelisted;
- timeout is enforced;
- output is scanned for host path / sensitive env / parent-process leaks.

OS-level isolation is detected (`sandbox-exec` on macOS, `firejail` on Linux)
and reported in metadata. C14 keeps execution on the soft backend so the
behavior is portable in CI; C14/C28 follow-ups can swap the runner backend
without changing the public result model.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

ViolationKind = Literal["path_leak", "env_leak", "process_leak", "network_attempt"]
ViolationSeverity = Literal["low", "medium", "high", "critical"]
IsolationBackend = Literal["sandbox-exec", "firejail", "soft"]

_OUTPUT_LIMIT = 128 * 1024
_ESSENTIAL_ENVS = {
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONPATH",
    "TMPDIR",
    "UV_CACHE_DIR",
    "VIRTUAL_ENV",
}
_SENSITIVE_ENV_NAME_PATTERNS = (
    "API_KEY",
    "AUTH_TOKEN",
    "DATABASE_URL",
    "DB_DSN",
    "PASSWORD",
    "PG_DSN",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)
_HOST_PATH_PATTERNS = (
    re.compile(r"/Users/[^ \n\t'\"]+"),
    re.compile(r"/home/[^ \n\t'\"]+"),
    re.compile(r"/private/[^ \n\t'\"]+"),
    re.compile(r"/etc/(passwd|shadow|ssh)[^ \n\t'\"]*"),
)
_PROCESS_PATTERNS = (
    re.compile(r"\bPPID\b", re.IGNORECASE),
    re.compile(r"\bparent process\b", re.IGNORECASE),
    re.compile(r"\bps\s+aux\b", re.IGNORECASE),
    re.compile(r"\b/proc/\d+\b"),
)
_NETWORK_PATTERNS = (
    re.compile(r"\bcurl\s+https?://", re.IGNORECASE),
    re.compile(r"\bwget\s+https?://", re.IGNORECASE),
    re.compile(r"\bsocket\.connect\b", re.IGNORECASE),
)


class EscapeViolation(BaseModel):
    kind: ViolationKind
    evidence: str
    severity: ViolationSeverity = "medium"


class SandboxResult(BaseModel):
    success: bool
    output: str
    cpu_ms: int
    violations: list[EscapeViolation] = Field(default_factory=list)
    tool_name: str = ""
    agent_id: str = ""
    returncode: int | None = None
    timed_out: bool = False
    backend: IsolationBackend = "soft"
    cwd: str = ""
    error: str | None = None


class ToolCallSandbox:
    """Run registered KUN skills in a constrained subprocess."""

    def __init__(
        self,
        allowed_paths: list[str],
        allowed_envs: list[str] | None = None,
        cpu_limit_sec: int = 30,
    ) -> None:
        if not allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        self.allowed_paths = [_resolve_existing_dir(path) for path in allowed_paths]
        self.allowed_envs = set(allowed_envs or ()) | _ESSENTIAL_ENVS
        self.cpu_limit_sec = max(1, min(300, int(cpu_limit_sec)))
        self.backend = detect_isolation_backend()

    async def run(self, tool_name: str, args: dict[str, Any], agent_id: str) -> SandboxResult:
        """Run a tool call in a subprocess and scan the output for escape signals."""
        if not tool_name.strip():
            raise ValueError("tool_name is required")
        cwd = self.allowed_paths[0]
        payload = {"tool_name": tool_name, "args": args}
        started = time.perf_counter()

        try:
            proc_result = await asyncio.to_thread(
                _run_dispatch_subprocess,
                payload,
                cwd,
                self._env(agent_id),
                self.cpu_limit_sec,
            )
        except subprocess.TimeoutExpired as e:
            output = _combine_output(e.stdout, e.stderr) or f"timed out after {self.cpu_limit_sec}s"
            return SandboxResult(
                success=False,
                output=output,
                cpu_ms=_elapsed_ms(started),
                violations=self.detect_escape(output),
                tool_name=tool_name,
                agent_id=agent_id,
                timed_out=True,
                backend=self.backend,
                cwd=str(cwd),
                error=f"timed out after {self.cpu_limit_sec}s",
            )

        output = _combine_output(proc_result.stdout, proc_result.stderr)
        violations = self.detect_escape(output)
        success = proc_result.returncode == 0 and not any(
            v.severity in {"high", "critical"} for v in violations
        )
        return SandboxResult(
            success=success,
            output=output,
            cpu_ms=_elapsed_ms(started),
            violations=violations,
            tool_name=tool_name,
            agent_id=agent_id,
            returncode=proc_result.returncode,
            backend=self.backend,
            cwd=str(cwd),
            error=None
            if proc_result.returncode == 0
            else f"tool subprocess exited {proc_result.returncode}",
        )

    def detect_escape(self, output: str) -> list[EscapeViolation]:
        """Detect likely host escape / leakage in tool output."""
        violations: list[EscapeViolation] = []
        text = output or ""

        for host_pattern in _HOST_PATH_PATTERNS:
            for match in host_pattern.findall(text):
                evidence = match if isinstance(match, str) else "/".join(match)
                if not _is_allowed_path_text(evidence, self.allowed_paths):
                    violations.append(
                        EscapeViolation(kind="path_leak", evidence=evidence, severity="high")
                    )

        for name, value in os.environ.items():
            if name in self.allowed_envs:
                continue
            if _looks_sensitive_env(name) and value and value in text:
                violations.append(
                    EscapeViolation(kind="env_leak", evidence=name, severity="critical")
                )
        for sensitive_pattern in _SENSITIVE_ENV_NAME_PATTERNS:
            if sensitive_pattern in text and sensitive_pattern not in self.allowed_envs:
                violations.append(
                    EscapeViolation(kind="env_leak", evidence=sensitive_pattern, severity="high")
                )

        for process_pattern in _PROCESS_PATTERNS:
            match = process_pattern.search(text)
            if match is not None:
                violations.append(
                    EscapeViolation(kind="process_leak", evidence=match.group(0), severity="medium")
                )

        for network_pattern in _NETWORK_PATTERNS:
            match = network_pattern.search(text)
            if match is not None:
                violations.append(
                    EscapeViolation(
                        kind="network_attempt", evidence=match.group(0), severity="medium"
                    )
                )

        return _dedupe_violations(violations)

    def _env(self, agent_id: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key in self.allowed_envs}
        env.update(
            {
                "KUN_SANDBOX": "1",
                "KUN_SANDBOX_BACKEND": self.backend,
                "KUN_SANDBOX_AGENT_ID": agent_id,
                "KUN_NETWORK_DISABLED": "1",
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            }
        )
        return env


def detect_isolation_backend() -> IsolationBackend:
    """Detect available strong-isolation command, falling back to soft mode."""
    system = platform.system().lower()
    if system == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if system == "linux" and shutil.which("firejail"):
        return "firejail"
    return "soft"


def _run_dispatch_subprocess(
    payload: dict[str, Any],
    cwd: Path,
    env: dict[str, str],
    timeout_sec: int,
) -> subprocess.CompletedProcess[str]:
    script = (
        "import asyncio,json,sys\n"
        "from kun.skills.dispatcher import autoload_builtins, dispatch\n"
        "async def main():\n"
        "    payload=json.load(sys.stdin)\n"
        "    autoload_builtins()\n"
        "    result=await dispatch(payload['tool_name'], payload.get('args') or {})\n"
        "    print(result.model_dump_json())\n"
        "asyncio.run(main())\n"
    )
    stdin = json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [sys.executable, "-c", script],
        input=stdin,
        capture_output=True,
        cwd=str(cwd),
        env=env,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


def _resolve_existing_dir(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"allowed path must be an existing directory: {path}")
    return resolved


def _combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = [_to_text(stdout), _to_text(stderr)]
    text = "\n".join(part for part in parts if part)
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[:_OUTPUT_LIMIT]


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _looks_sensitive_env(name: str) -> bool:
    upper = name.upper()
    return any(pattern in upper for pattern in _SENSITIVE_ENV_NAME_PATTERNS)


def _is_allowed_path_text(path_text: str, allowed_paths: list[Path]) -> bool:
    try:
        candidate = Path(path_text).expanduser().resolve()
    except OSError:
        return False
    return any(root == candidate or root in candidate.parents for root in allowed_paths)


def _dedupe_violations(violations: list[EscapeViolation]) -> list[EscapeViolation]:
    seen: set[tuple[str, str]] = set()
    deduped: list[EscapeViolation] = []
    for violation in violations:
        key = (violation.kind, violation.evidence)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(violation)
    return deduped


__all__ = [
    "EscapeViolation",
    "IsolationBackend",
    "SandboxResult",
    "ToolCallSandbox",
    "detect_isolation_backend",
]
