"""shell-exec skill — run a shell command with a timeout.

Params:
  command: str (required) — shell command (passed to /bin/sh -c)
  timeout_sec: float (default 30, max 300)
  cwd: str (optional, resolved under KUN_SKILL_EXEC_ROOTS)

Returns:
  {stdout, stderr, returncode, truncated}

Sandbox: command execution is restricted to configured executable-skill roots
(``KUN_SKILL_EXEC_ROOTS`` or ``KUN_SKILL_EXEC_ROOT``; default
``/tmp/kun-skill-exec``). This is an auditable workspace boundary, not a chroot
or container boundary.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kun.skills.dispatcher import SkillResult, register
from kun.skills.sandbox import SandboxPathError, resolve_execution_cwd, sandbox_metadata

_OUTPUT_LIMIT = 65536


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    command = str(params.get("command") or "").strip()
    if not command:
        return SkillResult(skill_id="shell-exec", ok=False, error="command is required")

    timeout = max(1.0, min(300.0, float(params.get("timeout_sec") or 30)))
    try:
        cwd = resolve_execution_cwd(params.get("cwd") or None)
    except SandboxPathError as exc:
        return SkillResult(
            skill_id="shell-exec",
            ok=False,
            error=str(exc),
            duration_sec=time.perf_counter() - started,
        )

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return SkillResult(
            skill_id="shell-exec",
            ok=False,
            error=f"timed out after {timeout}s",
            duration_sec=time.perf_counter() - started,
        )

    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    truncated = False
    if len(stdout) > _OUTPUT_LIMIT:
        stdout = stdout[:_OUTPUT_LIMIT]
        truncated = True
    if len(stderr) > _OUTPUT_LIMIT:
        stderr = stderr[:_OUTPUT_LIMIT]
        truncated = True

    return SkillResult(
        skill_id="shell-exec",
        ok=proc.returncode == 0,
        output={
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode or 0,
            "truncated": truncated,
        },
        duration_sec=time.perf_counter() - started,
        metadata={
            "command": command[:80],
            "timeout_sec": timeout,
            **sandbox_metadata(cwd),
        },
    )


register("shell-exec", execute)
