"""Generate append-only PROMISES.md sections from git commits."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TOKEN_RE = re.compile(
    r"\b(?:Wire\s*[-#]?\s*\d+[A-Z]?|C\d+|T\d+|BATCH\d+|V\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommitPromise:
    commit: str
    subject: str
    refs: list[str] = field(default_factory=list)


def extract_refs(subject: str) -> list[str]:
    """Extract task-ish refs from one commit subject."""

    refs: list[str] = []
    for match in TOKEN_RE.finditer(subject):
        token = _normalize_ref(match.group(0))
        if token not in refs:
            refs.append(token)
    return refs


def parse_git_log_lines(lines: list[str]) -> list[CommitPromise]:
    """Parse ``git log --pretty=%h%x09%s`` style lines."""

    commits: list[CommitPromise] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if "\t" in text:
            commit, subject = text.split("\t", 1)
        else:
            parts = text.split(" ", 1)
            if len(parts) != 2:
                continue
            commit, subject = parts
        commits.append(CommitPromise(commit=commit, subject=subject, refs=extract_refs(subject)))
    return commits


def collect_git_commits(
    *,
    rev_range: str,
    cwd: str | Path = ".",
) -> list[CommitPromise]:
    """Collect commits from git without touching PROMISES.md."""

    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("git executable not found")
    completed = subprocess.run(
        [git_executable, "log", "--pretty=format:%h%x09%s", rev_range],
        cwd=str(cwd),
        check=True,
        text=True,
        capture_output=True,
    )
    return parse_git_log_lines(completed.stdout.splitlines())


def render_promises_section(
    commits: list[CommitPromise],
    *,
    title: str = "自动生成承诺更新",
    generated_at: datetime | None = None,
) -> str:
    """Render a markdown section that can be appended to docs/PROMISES.md."""

    now = generated_at or datetime.now(UTC)
    lines = [
        f"## {title}",
        "",
        f"> 自动生成时间: {now.isoformat()}",
        "> 来源: git log commit subject. 请人工 review 后保留。",
        "",
        "| commit | refs | 摘要 |",
        "|---|---|---|",
    ]
    if not commits:
        lines.append("| - | - | 本范围没有 commit |")
    for commit in commits:
        refs = ", ".join(commit.refs) if commit.refs else "-"
        lines.append(f"| `{commit.commit}` | {refs} | {commit.subject} |")
    lines.append("")
    return "\n".join(lines)


def append_promises_section(path: str | Path, section: str) -> None:
    """Append a rendered section to PROMISES.md."""

    target = Path(path)
    existing = target.read_text() if target.exists() else ""
    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    target.write_text(f"{existing}{separator}{section}")


def generate_promises_section(
    *,
    rev_range: str,
    cwd: str | Path = ".",
    title: str = "自动生成承诺更新",
    generated_at: datetime | None = None,
) -> str:
    commits = collect_git_commits(rev_range=rev_range, cwd=cwd)
    return render_promises_section(commits, title=title, generated_at=generated_at)


def _normalize_ref(token: str) -> str:
    compact = re.sub(r"\s+", "", token.strip())
    compact = compact.replace("#", "").replace("-", "")
    if compact.lower().startswith("wire"):
        return "Wire" + compact[4:]
    return compact.upper()


__all__ = [
    "CommitPromise",
    "append_promises_section",
    "collect_git_commits",
    "extract_refs",
    "generate_promises_section",
    "parse_git_log_lines",
    "render_promises_section",
]
