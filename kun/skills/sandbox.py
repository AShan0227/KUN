"""Shared sandbox helpers for executable KUN skills.

Executable skills are useful, but they should not silently run from whatever
host directory the current process happens to use.  This helper gives shell and
Python execution a small, auditable workspace boundary while still allowing
Control Plane deployments to opt specific project roots in through environment
configuration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DEFAULT_EXEC_ROOT = "/tmp/kun-skill-exec"  # noqa: S108 - explicit local sandbox root


class SandboxPathError(ValueError):
    """Raised when a requested execution cwd escapes the configured sandbox."""


def execution_roots() -> list[Path]:
    """Return configured executable-skill roots, creating them as needed."""

    raw = os.getenv("KUN_SKILL_EXEC_ROOTS") or os.getenv("KUN_SKILL_EXEC_ROOT") or _DEFAULT_EXEC_ROOT
    roots: list[Path] = []
    for part in raw.split(":"):
        if not part.strip():
            continue
        root = Path(part).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        roots.append(root)
    if roots:
        return roots
    root = Path(_DEFAULT_EXEC_ROOT).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return [root]


def resolve_execution_cwd(raw_cwd: Any | None = None) -> Path:
    """Resolve a cwd for executable skills under configured roots.

    Relative paths are interpreted under the first root.  Absolute paths must be
    inside one configured root.  This is not a container, but it prevents prompt
    injected code from accidentally running in arbitrary host directories.
    """

    roots = execution_roots()
    primary = roots[0]
    if raw_cwd in {None, ""}:
        return primary
    requested = Path(str(raw_cwd)).expanduser()
    candidate = requested.resolve() if requested.is_absolute() else (primary / requested).resolve()
    if any(candidate == root or root in candidate.parents for root in roots):
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        return candidate
    raise SandboxPathError(
        f"cwd escapes executable skill sandbox: {candidate}; allowed roots: "
        + ", ".join(str(root) for root in roots)
    )


def sandbox_metadata(cwd: Path) -> dict[str, object]:
    """Return audit metadata for one executable-skill run."""

    return {
        "cwd": str(cwd),
        "sandbox_roots": [str(root) for root in execution_roots()],
        "sandbox_enforced": True,
    }


__all__ = [
    "SandboxPathError",
    "execution_roots",
    "resolve_execution_cwd",
    "sandbox_metadata",
]
