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


@dataclass(frozen=True)
class ReleaseNoteGroup:
    section: str
    title: str
    commits: list[CommitPromise] = field(default_factory=list)


V22_SECTION_TITLES: dict[str, str] = {
    "§19": "决策核心：边际收益 + 按需扩展",
    "§20": "知识图谱 + 导航式记忆",
    "§21": "ExecutionMode FAST/SMART/MAX/ENSEMBLE",
    "§22": "Hermes 结构化执行协议",
    "§23": "输入翻译器 / 真实世界交互层",
    "§24": "CodeCapability 代码能力层",
    "§25": "信用分配 + 稀疏奖励",
    "§26": "KUN-Lab 内测版",
    "§27": "推理时反思 + 学习成长区",
    "§28": "TaskBoundaryGuard 任务边界守护",
    "release": "发版与运维配套",
    "other": "其他 V2.2 收尾",
}


V22_REF_TO_SECTION: dict[str, str] = {
    "C32": "§21",
    "C33": "§28",
    "C34": "§22",
    "C35": "§26",
    "C36": "§26",
    "C37": "§20",
    "C38": "§20",
    "C39": "§20",
    "C40": "§26",
    "C41": "§20",
    "C42": "§20",
    "C43": "§23",
    "C44": "§19",
    "C45": "§26",
    "C46": "§26",
    "C47": "§26",
    "C48": "§26",
    "C49": "release",
    "C50": "release",
    "C51": "release",
    "C52": "release",
    "C53": "release",
}


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


def infer_v22_section(commit: CommitPromise) -> str:
    """Infer the V2.2 changelog section for a commit subject."""

    subject = commit.subject.lower()
    explicit_section = re.search(r"§\s*(1[9]|2[0-8])", commit.subject)
    if explicit_section:
        return "§" + explicit_section.group(1)
    for ref in commit.refs:
        section = V22_REF_TO_SECTION.get(ref)
        if section:
            return section
    keyword_sections = [
        ("ensemble", "§21"),
        ("executionmode", "§21"),
        ("graph", "§20"),
        ("relationship", "§20"),
        ("panorama", "§20"),
        ("translator", "§23"),
        ("input", "§23"),
        ("codecapability", "§24"),
        ("codereader", "§24"),
        ("codewriter", "§24"),
        ("codedebugger", "§24"),
        ("codereviewer", "§24"),
        ("credit", "§25"),
        ("reward", "§25"),
        ("lab", "§26"),
        ("recipe", "§26"),
        ("benchmark", "§26"),
        ("dogfood", "§26"),
        ("rethink", "§27"),
        ("judge", "§27"),
        ("boundary", "§28"),
        ("offtopic", "§28"),
        ("incident", "§19"),
        ("value", "§19"),
        ("changelog", "release"),
        ("release", "release"),
        ("promises", "release"),
    ]
    compact = subject.replace("-", "").replace("_", "")
    for keyword, section in keyword_sections:
        if keyword in compact:
            return section
    return "other"


def group_release_notes(commits: list[CommitPromise]) -> list[ReleaseNoteGroup]:
    """Group commits by the V2.2 section they belong to."""

    grouped: dict[str, list[CommitPromise]] = {key: [] for key in V22_SECTION_TITLES}
    for commit in commits:
        grouped.setdefault(infer_v22_section(commit), []).append(commit)
    groups: list[ReleaseNoteGroup] = []
    for section, title in V22_SECTION_TITLES.items():
        entries = grouped.get(section, [])
        if entries:
            groups.append(ReleaseNoteGroup(section=section, title=title, commits=entries))
    return groups


def render_release_notes(
    commits: list[CommitPromise],
    *,
    version: str = "v2.2.0",
    generated_at: datetime | None = None,
) -> str:
    """Render reviewable markdown release notes for a git commit range."""

    now = generated_at or datetime.now(UTC)
    groups = group_release_notes(commits)
    lines = [
        f"# KUN {version} Changelog",
        "",
        f"> 自动生成时间: {now.isoformat()}",
        "> 来源: git log commit subject. 打 tag 前请人工 review。",
        "",
        "## 总览",
        "",
        f"- commit 数: {len(commits)}",
        f"- V2.2 分组数: {len(groups)}",
        "",
    ]
    if not commits:
        lines.extend(["## 无变更", "", "这个范围没有 commit。", ""])
        return "\n".join(lines)

    for group in groups:
        lines.extend([f"## {group.section} {group.title}", ""])
        for commit in group.commits:
            refs = ", ".join(commit.refs) if commit.refs else "-"
            lines.append(f"- `{commit.commit}` [{refs}] {commit.subject}")
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


def generate_release_notes(
    *,
    rev_range: str,
    cwd: str | Path = ".",
    version: str = "v2.2.0",
    generated_at: datetime | None = None,
) -> str:
    commits = collect_git_commits(rev_range=rev_range, cwd=cwd)
    return render_release_notes(commits, version=version, generated_at=generated_at)


def _normalize_ref(token: str) -> str:
    compact = re.sub(r"\s+", "", token.strip())
    compact = compact.replace("#", "").replace("-", "")
    if compact.lower().startswith("wire"):
        return "Wire" + compact[4:]
    return compact.upper()


__all__ = [
    "CommitPromise",
    "ReleaseNoteGroup",
    "append_promises_section",
    "collect_git_commits",
    "extract_refs",
    "generate_promises_section",
    "generate_release_notes",
    "group_release_notes",
    "infer_v22_section",
    "parse_git_log_lines",
    "render_promises_section",
    "render_release_notes",
]
