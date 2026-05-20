"""python-exec skill — run a Python snippet in a subprocess with a timeout.

Params:
  code: str (required) — Python source code
  timeout_sec: float (default 30, max 300)
  stdin: str (optional) — sent to the subprocess on stdin
  cwd: str (optional, resolved under KUN_SKILL_EXEC_ROOTS)

Returns:
  {stdout, stderr, returncode, truncated}

Sandbox: subprocess execution is restricted to configured executable-skill
roots (``KUN_SKILL_EXEC_ROOTS`` or ``KUN_SKILL_EXEC_ROOT``; default
``/tmp/kun-skill-exec``). This is an auditable workspace boundary, not a chroot
or container boundary.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kun.skills.dispatcher import SkillResult, register
from kun.skills.sandbox import SandboxPathError, resolve_execution_cwd, sandbox_metadata

_OUTPUT_LIMIT = 65536  # 64 KiB per stream


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    code = str(params.get("code") or "").strip()
    if not code:
        return SkillResult(skill_id="python-exec", ok=False, error="code is required")

    timeout = max(1.0, min(300.0, float(params.get("timeout_sec") or 30)))
    stdin_data = str(params.get("stdin") or "")
    try:
        cwd = resolve_execution_cwd(params.get("cwd") or None)
    except SandboxPathError as exc:
        return SkillResult(
            skill_id="python-exec",
            ok=False,
            error=str(exc),
            duration_sec=time.perf_counter() - started,
        )

    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8") if stdin_data else None),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return SkillResult(
            skill_id="python-exec",
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
        skill_id="python-exec",
        ok=proc.returncode == 0,
        output={
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode or 0,
            "truncated": truncated,
        },
        duration_sec=time.perf_counter() - started,
        metadata={
            "timeout_sec": timeout,
            **sandbox_metadata(cwd),
        },
    )


register("python-exec", execute)
