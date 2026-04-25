"""shell-exec skill — run a shell command with a timeout.

Params:
  command: str (required) — shell command (passed to /bin/sh -c)
  timeout_sec: float (default 30, max 300)
  cwd: str (optional)

Returns:
  {stdout, stderr, returncode, truncated}

Sandbox: this is the same trust level as python-exec — it's an agent
escape hatch, not a security boundary. Wrap with a container if you need
isolation from untrusted prompts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from kun.skills.dispatcher import SkillResult, register

_OUTPUT_LIMIT = 65536


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    command = str(params.get("command") or "").strip()
    if not command:
        return SkillResult(skill_id="shell-exec", ok=False, error="command is required")

    timeout = max(1.0, min(300.0, float(params.get("timeout_sec") or 30)))
    cwd = params.get("cwd") or None

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
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
        metadata={"command": command[:80], "timeout_sec": timeout},
    )


register("shell-exec", execute)
