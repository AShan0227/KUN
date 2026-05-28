"""grep-verify skill — structured grep for "verify before assume" pattern.

Designed to encode Claude Code's "grep verify before assume" engineering
discipline as a first-class KUN skill. Instead of telling the LLM to use
shell-exec with raw grep (error-prone, needs shell escaping, no schema),
``grep-verify`` is a curated primitive:

  - Input: pattern (regex), root (whitelist dir), optional file_glob
  - Output: structured ``{matches: [{path, line_no, line}], match_count, verdict}``
  - verdict: "confirmed" if matches > 0, "refuted" if 0

The skill is scoped to the repo's READ whitelist (same as self-reflect:
docs/seeds/kun/tests/scripts/alembic). It can NOT execute arbitrary shell,
can NOT escape sandbox, can NOT mutate files. Pure observation tool.

Params:
  pattern: str (required) — regex (POSIX extended, passed to grep -E)
  root: str (default "kun") — search root, must be under the whitelist
  file_glob: str (default "*.py") — file extension filter
  max_matches: int (default 50) — cap to prevent context blow-up
  claim: str (optional) — free-text description of what you're trying to verify

Returns:
  - confirmed when matches > 0: caller's assumption is supported
  - refuted when matches == 0: caller's assumption needs revision

Pairs with seeds/methodologies/grep_verify_before_assume.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from kun.core.logging import get_logger
from kun.skills.dispatcher import SkillResult, register

log = get_logger("kun.skills.grep_verify")

# Whitelist dirs (relative to repo root). MUST match self-reflect's read
# whitelist — both are "look at KUN's own code/docs" tools.
_WHITELIST_DIRS: tuple[str, ...] = (
    "docs",
    "seeds",
    "kun",
    "tests",
    "scripts",
    "alembic",
)
_DEFAULT_ROOT = "kun"
_DEFAULT_GLOB = "*.py"
_DEFAULT_MAX = 50

# Regex sanity — reject patterns with shell metacharacters that might escape
# the argv layer. We pass via subprocess.run([...]) not shell, so this is
# defense-in-depth, not primary safety.
_FORBIDDEN_PATTERN_CHARS = re.compile(r"[`$;|&><\n]")


def _repo_root() -> Path:
    import os
    env = os.getenv("KUN_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3]


def _is_whitelisted(root: str) -> bool:
    if ".." in Path(root).parts:
        return False
    top = Path(root).parts[0] if Path(root).parts else ""
    return top in _WHITELIST_DIRS


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    pattern = str(params.get("pattern") or "").strip()
    root = str(params.get("root") or _DEFAULT_ROOT).strip()
    file_glob = str(params.get("file_glob") or _DEFAULT_GLOB).strip()
    max_raw = params.get("max_matches", _DEFAULT_MAX)
    claim = str(params.get("claim") or "").strip()

    if not pattern:
        return SkillResult(
            skill_id="grep-verify", ok=False, error="pattern is required"
        )
    if _FORBIDDEN_PATTERN_CHARS.search(pattern):
        return SkillResult(
            skill_id="grep-verify",
            ok=False,
            error="pattern contains forbidden shell metacharacters",
        )
    if not _is_whitelisted(root):
        return SkillResult(
            skill_id="grep-verify",
            ok=False,
            error=f"root not in whitelist (allowed: {_WHITELIST_DIRS}): {root}",
        )

    try:
        max_matches = int(max_raw) if isinstance(max_raw, (int, str)) else _DEFAULT_MAX
    except (TypeError, ValueError):
        max_matches = _DEFAULT_MAX
    max_matches = min(max(max_matches, 1), 500)

    repo = _repo_root()
    target = (repo / root).resolve()
    try:
        target.relative_to(repo)
    except ValueError:
        return SkillResult(
            skill_id="grep-verify", ok=False, error=f"root escapes repo: {root}"
        )

    if not target.exists():
        return SkillResult(
            skill_id="grep-verify", ok=False, error=f"root not found: {root}"
        )

    grep_bin = shutil.which("grep")
    if not grep_bin:
        return SkillResult(
            skill_id="grep-verify", ok=False, error="grep binary not found on PATH"
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            grep_bin,
            "-rnE",
            "--include",
            file_glob,
            pattern,
            str(target),
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, _stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return SkillResult(skill_id="grep-verify", ok=False, error="grep timed out")
    except FileNotFoundError:
        return SkillResult(
            skill_id="grep-verify", ok=False, error="grep binary not found on PATH"
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    matches: list[dict[str, Any]] = []
    # Compute repo prefix once (string ops only — no per-match Path.resolve syscall)
    repo_prefix = str(repo) + "/"
    for line in stdout.splitlines():
        # grep -rn output: "path:line_no:matching line"
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path_str, line_no_str, match_text = parts
        # Strip the repo prefix to get a relative path; fall back to raw if not prefixed
        rel_path = path_str[len(repo_prefix):] if path_str.startswith(repo_prefix) else path_str
        try:
            line_no = int(line_no_str)
        except ValueError:
            continue
        matches.append(
            {
                "path": rel_path,
                "line_no": line_no,
                "line": match_text.rstrip(),
            }
        )
        if len(matches) >= max_matches:
            break

    verdict = "confirmed" if matches else "refuted"
    log.info(
        "grep_verify.executed",
        pattern=pattern[:80],
        root=root,
        match_count=len(matches),
        verdict=verdict,
        claim=claim[:120] if claim else None,
    )

    return SkillResult(
        skill_id="grep-verify",
        ok=True,
        output={
            "verdict": verdict,
            "match_count": len(matches),
            "matches": matches,
            "truncated": len(matches) >= max_matches,
            "claim": claim or None,
        },
        duration_sec=time.perf_counter() - started,
    )


register("grep-verify", execute)
