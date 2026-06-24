"""Command-level policy for the shell-exec skill (audit F035).

shell-exec previously ran any LLM-produced command through ``/bin/sh -c`` with
**no command-level filtering** — the cwd sandbox bounds *where* it runs, not
*what* it runs. This adds a command-level guard rail:

  - a default-on denylist of catastrophic one-liners (``rm -rf /``, fork bombs,
    ``mkfs``, ``dd of=/dev/...``, ``shutdown`` …) — blocks the obvious foot-guns
    out of the box without touching benign commands (echo / ls / pytest …);
  - ``KUN_SHELL_EXEC_DENY`` — operator-supplied extra deny regexes
    (newline- or ``;;``-separated);
  - ``KUN_SHELL_EXEC_ALLOW`` — if set, switches to allowlist mode: only commands
    whose first token's basename is listed may run (fail-closed).

This is a guard rail, NOT a sandbox. Real isolation (container / seccomp) and
the long-dead per-manifest ``allowed_commands`` field are tracked in F035a.
"""

from __future__ import annotations

import os
import re
import shlex

__all__ = ["CommandRejectedError", "check_shell_command"]


class CommandRejectedError(Exception):
    """Raised when a shell command violates the command policy."""


# Catastrophic patterns blocked by default. Conservative on purpose — these
# target filesystem-root / device / power operations a legitimate skill never
# needs, so benign commands (incl. rm of a sandbox subpath) are unaffected.
_DEFAULT_DENY: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r":\(\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:",  # classic fork bomb
        # rm targeting filesystem root / home (not an arbitrary absolute path)
        r"\brm\b(?:\s+-\S+)*\s+(?:/|/\*|~|~/|\$HOME|\$HOME/)(?:\s|$)",
        r"\bmkfs(?:\.\w+)?\b",  # format a filesystem
        r"\bdd\b[^\n]*\bof=/dev/",  # overwrite a block device
        r">\s*/dev/(?:sd|nvme|hd|disk|mmcblk)",  # clobber a disk
        r"\b(?:shutdown|reboot|halt|poweroff)\b",  # power control
        r"\binit\s+0\b",
    )
)


def _extra_deny() -> list[re.Pattern[str]]:
    raw = os.getenv("KUN_SHELL_EXEC_DENY", "")
    parts = [p.strip() for chunk in raw.split(";;") for p in chunk.splitlines()]
    out: list[re.Pattern[str]] = []
    for p in parts:
        if not p:
            continue
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            # A malformed operator regex must not silently disable the policy;
            # fall back to a literal-substring match.
            out.append(re.compile(re.escape(p), re.IGNORECASE))
    return out


def _allowlist() -> set[str] | None:
    raw = os.getenv("KUN_SHELL_EXEC_ALLOW", "").strip()
    if not raw:
        return None
    tokens = {t.strip() for chunk in raw.replace(",", " ").split() for t in [chunk] if t.strip()}
    return tokens or None


def check_shell_command(command: str) -> None:
    """Raise CommandRejectedError if ``command`` violates the command policy.

    Order: default denylist → operator denylist → optional allowlist mode.
    """
    cmd = (command or "").strip()
    if not cmd:
        return

    for pat in _DEFAULT_DENY:
        if pat.search(cmd):
            raise CommandRejectedError(f"command blocked by default safety denylist: {pat.pattern!r}")
    for pat in _extra_deny():
        if pat.search(cmd):
            raise CommandRejectedError("command blocked by KUN_SHELL_EXEC_DENY policy")

    allow = _allowlist()
    if allow is not None:
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            # Unparseable command under allowlist mode → fail closed.
            raise CommandRejectedError("command not parseable under allowlist mode") from None
        first = os.path.basename(tokens[0]) if tokens else ""
        if first not in allow:
            raise CommandRejectedError(f"command {first!r} not in KUN_SHELL_EXEC_ALLOW allowlist")
