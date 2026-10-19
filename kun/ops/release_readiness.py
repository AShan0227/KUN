"""Formal release/tag readiness checks.

This is stricter than the general production preflight: it checks the local git
state, tag shape, rollback/hotfix notes, legal guard, migrations, and the
existing deployment gates before a release tag is cut.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.config import Settings
from kun.engineering.delivery_status import delivery_status_summary, validate_delivery_status
from kun.ops.preflight import PreflightReport, run_preflight

ReleaseCheckSeverity = Literal["ok", "warn", "blocker"]

_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-(?:rc|hotfix)\.\d+)?$")


class ReleaseReadinessCheck(BaseModel):
    """One formal release gate check."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    severity: ReleaseCheckSeverity
    title: str
    detail: str
    suggested_action: str = ""


class ReleaseReadinessReport(BaseModel):
    """Aggregated release readiness report."""

    model_config = ConfigDict(extra="forbid")

    version: str
    status: Literal["pass", "warn", "block"]
    checks: list[ReleaseReadinessCheck] = Field(default_factory=list)
    delivery_summary: dict[str, int] = Field(default_factory=dict)
    preflight_status: str = ""

    @property
    def blockers(self) -> list[ReleaseReadinessCheck]:
        return [check for check in self.checks if check.severity == "blocker"]

    @property
    def warnings(self) -> list[ReleaseReadinessCheck]:
        return [check for check in self.checks if check.severity == "warn"]


def run_release_readiness(
    *,
    version: str,
    repo_root: Path | None = None,
    cfg: Settings | None = None,
    run_alembic_heads: bool = True,
    run_legal_guard: bool = True,
    allow_dirty: bool = False,
) -> ReleaseReadinessReport:
    """Run local release gates before creating a tag."""

    root = (repo_root or Path.cwd()).resolve()
    checks: list[ReleaseReadinessCheck] = []
    checks.append(_tag_name_check(version))
    checks.extend(_git_checks(root, version=version, allow_dirty=allow_dirty))
    checks.append(_local_alembic_single_head_check(root))
    checks.extend(_backup_restore_checks(root))
    checks.append(_release_notes_check(root))
    checks.append(_release_checklist_check(root))
    checks.append(_delivery_status_check())
    if run_legal_guard:
        checks.append(_legal_guard_check(root))

    preflight = run_preflight(cfg=cfg, repo_root=root, run_alembic_heads=run_alembic_heads)
    checks.append(_preflight_check(preflight))

    if any(check.severity == "blocker" for check in checks):
        status: Literal["pass", "warn", "block"] = "block"
    elif any(check.severity == "warn" for check in checks):
        status = "warn"
    else:
        status = "pass"
    return ReleaseReadinessReport(
        version=version,
        status=status,
        checks=checks,
        delivery_summary=delivery_status_summary(),
        preflight_status=preflight.status,
    )


def _tag_name_check(version: str) -> ReleaseReadinessCheck:
    ok = bool(_TAG_RE.fullmatch(version))
    return ReleaseReadinessCheck(
        check_id="tag_name",
        severity="ok" if ok else "blocker",
        title="release tag 名称合法" if ok else "release tag 名称不合法",
        detail=version,
        suggested_action="" if ok else "使用 vX.Y.Z、vX.Y.Z-rc.N 或 vX.Y.Z-hotfix.N。",
    )


def _git_checks(
    root: Path,
    *,
    version: str,
    allow_dirty: bool,
) -> list[ReleaseReadinessCheck]:
    git = shutil.which("git")
    if git is None:
        return [
            ReleaseReadinessCheck(
                check_id="git_available",
                severity="blocker",
                title="git 不在 PATH",
                detail="无法检查 worktree 和 tag。",
                suggested_action="安装 git 后重跑 release-check。",
            )
        ]

    checks: list[ReleaseReadinessCheck] = []
    status = _run([git, "status", "--porcelain"], cwd=root)
    if status.returncode != 0:
        checks.append(
            ReleaseReadinessCheck(
                check_id="git_worktree_clean",
                severity="blocker",
                title="无法读取 git worktree 状态",
                detail=_proc_output(status),
                suggested_action="确认 repo_root 是 git 仓库。",
            )
        )
    else:
        dirty_lines = _non_empty_lines(status.stdout)
        if dirty_lines and not allow_dirty:
            checks.append(
                ReleaseReadinessCheck(
                    check_id="git_worktree_clean",
                    severity="blocker",
                    title="worktree 不干净",
                    detail=f"{len(dirty_lines)} 个未提交/未跟踪变更。",
                    suggested_action="正式打 tag 前提交、stash 或清理；hotfix 也必须可复现。",
                )
            )
        elif dirty_lines:
            checks.append(
                ReleaseReadinessCheck(
                    check_id="git_worktree_clean",
                    severity="warn",
                    title="worktree 不干净但已显式允许",
                    detail=f"{len(dirty_lines)} 个未提交/未跟踪变更。",
                    suggested_action="只在本地预演使用 --allow-dirty；正式 release 不要带脏树。",
                )
            )
        else:
            checks.append(
                ReleaseReadinessCheck(
                    check_id="git_worktree_clean",
                    severity="ok",
                    title="worktree 干净",
                    detail="git status --porcelain 无输出。",
                )
            )

    existing_tag = _run([git, "rev-parse", "-q", "--verify", f"refs/tags/{version}"], cwd=root)
    tag_exists = existing_tag.returncode == 0
    checks.append(
        ReleaseReadinessCheck(
            check_id="git_tag_available",
            severity="blocker" if tag_exists else "ok",
            title="release tag 已存在" if tag_exists else "release tag 尚未存在",
            detail=version,
            suggested_action="不要覆盖 tag；改版本号或确认这是 rollback/hotfix 操作。"
            if tag_exists
            else "",
        )
    )
    branch = _run([git, "branch", "--show-current"], cwd=root)
    branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
    checks.append(
        ReleaseReadinessCheck(
            check_id="git_branch",
            severity="ok" if branch_name else "warn",
            title="当前分支已识别" if branch_name else "当前处于 detached HEAD",
            detail=branch_name or "detached HEAD",
            suggested_action=""
            if branch_name
            else "正式 release 建议从受保护 release/main 分支切 tag。",
        )
    )
    return checks


def _local_alembic_single_head_check(root: Path) -> ReleaseReadinessCheck:
    versions_dir = root / "alembic" / "versions"
    if not versions_dir.is_dir():
        return ReleaseReadinessCheck(
            check_id="alembic_local_single_head",
            severity="blocker",
            title="找不到 Alembic versions 目录",
            detail=str(versions_dir),
            suggested_action="确认 release-check 在仓库根目录执行。",
        )
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    errors: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        revision = _module_literal(tree, "revision")
        down_revision = _module_literal(tree, "down_revision")
        if isinstance(revision, str):
            revisions.add(revision)
        else:
            errors.append(f"{path.name}: revision 缺失")
        down_revisions.update(_down_revision_values(down_revision))
    if errors:
        return ReleaseReadinessCheck(
            check_id="alembic_local_single_head",
            severity="blocker",
            title="Alembic revision 元数据不可解析",
            detail="；".join(errors[:5]),
            suggested_action="修复 migration revision/down_revision。",
        )
    heads = sorted(revisions - down_revisions)
    return ReleaseReadinessCheck(
        check_id="alembic_local_single_head",
        severity="ok" if len(heads) == 1 else "blocker",
        title="Alembic 本地单 head" if len(heads) == 1 else "Alembic 本地不是单 head",
        detail="；".join(heads) if heads else "没有 head",
        suggested_action="" if len(heads) == 1 else "合并 migration head 后再 release。",
    )


def _backup_restore_checks(root: Path) -> list[ReleaseReadinessCheck]:
    required = {
        "scripts/backup_postgres.sh": "Postgres 备份脚本",
        "scripts/restore_postgres_smoke.sh": "Postgres restore smoke 脚本",
        "scripts/backup_restore_drill.py": "本地备份/恢复演练脚本",
    }
    missing = [rel for rel in required if not (root / rel).exists()]
    return [
        ReleaseReadinessCheck(
            check_id="backup_restore_prerequisites",
            severity="blocker" if missing else "ok",
            title="备份/恢复前置检查通过" if not missing else "备份/恢复前置脚本缺失",
            detail="；".join(f"{label}: {rel}" for rel, label in required.items()),
            suggested_action="" if not missing else "补齐缺失脚本: " + ", ".join(missing),
        )
    ]


def _release_notes_check(root: Path) -> ReleaseReadinessCheck:
    candidates = sorted([*root.glob("CHANGELOG-v*.md"), root / "CHANGELOG.md"])
    existing = [path.name for path in candidates if path.exists()]
    return ReleaseReadinessCheck(
        check_id="release_notes",
        severity="ok" if existing else "warn",
        title="release notes 文件存在" if existing else "未找到 release notes 文件",
        detail=", ".join(existing) if existing else "CHANGELOG-v*.md / CHANGELOG.md 都不存在",
        suggested_action="" if existing else "打 tag 前生成并人工 review release notes。",
    )


def _release_checklist_check(root: Path) -> ReleaseReadinessCheck:
    path = root / "docs" / "ops" / "release-checklist-v4.md"
    if not path.exists():
        return ReleaseReadinessCheck(
            check_id="release_checklist_v4",
            severity="blocker",
            title="缺少 V4 release checklist",
            detail=str(path),
            suggested_action="补 docs/ops/release-checklist-v4.md，写清 tag/rollback/hotfix/backup restore。",
        )
    text = path.read_text(encoding="utf-8").lower()
    required = ("tag", "rollback", "hotfix", "backup", "restore")
    missing = [word for word in required if word not in text]
    return ReleaseReadinessCheck(
        check_id="release_checklist_v4",
        severity="ok" if not missing else "blocker",
        title="V4 release checklist 覆盖 tag/rollback/hotfix/backup/restore"
        if not missing
        else "V4 release checklist 内容不完整",
        detail=str(path) if not missing else "缺少关键词: " + ", ".join(missing),
        suggested_action=""
        if not missing
        else "补齐 tag、rollback、hotfix、backup/restore 前置检查。",
    )


def _delivery_status_check() -> ReleaseReadinessCheck:
    issues = validate_delivery_status()
    return ReleaseReadinessCheck(
        check_id="delivery_status_validation",
        severity="ok" if not issues else "blocker",
        title="delivery-status validation 通过"
        if not issues
        else "delivery-status validation 失败",
        detail="无问题" if not issues else "；".join(issues),
        suggested_action="" if not issues else "修正 delivery_status 的伪 ready/证据缺失。",
    )


def _legal_guard_check(root: Path) -> ReleaseReadinessCheck:
    script = root / "scripts" / "check_legal_guard.py"
    if not script.exists():
        return ReleaseReadinessCheck(
            check_id="legal_guard",
            severity="blocker",
            title="Legal/IP guard 脚本缺失",
            detail=str(script),
            suggested_action="补回脚本或在 release checklist 里明确替代门禁。",
        )
    proc = _run([sys.executable, str(script)], cwd=root)
    return ReleaseReadinessCheck(
        check_id="legal_guard",
        severity="ok" if proc.returncode == 0 else "blocker",
        title="Legal/IP guard 通过" if proc.returncode == 0 else "Legal/IP guard 失败",
        detail=_proc_output(proc)[:800],
        suggested_action=""
        if proc.returncode == 0
        else "修复 LICENSE/NOTICE/README/IP policy 后再 release。",
    )


def _preflight_check(preflight: PreflightReport) -> ReleaseReadinessCheck:
    if preflight.status == "pass":
        severity: ReleaseCheckSeverity = "ok"
    elif preflight.status == "warn":
        severity = "warn"
    else:
        severity = "blocker"
    detail_items = [f"{item.check_id}:{item.title}" for item in preflight.blockers[:8]]
    if not detail_items:
        detail_items = [f"preflight status={preflight.status}"]
    return ReleaseReadinessCheck(
        check_id="production_preflight",
        severity=severity,
        title="production preflight 通过"
        if severity == "ok"
        else "production preflight 未完全通过",
        detail="；".join(detail_items),
        suggested_action=""
        if severity == "ok"
        else "先修复 preflight blocker/warn，再打正式 tag。",
    )


def _module_literal(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        target_names: list[str] = []
        value: ast.expr | None
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                target_names.append(node.target.id)
            value = node.value
        else:
            target_names.extend(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
            value = node.value
        if name not in target_names or value is None:
            continue
        try:
            return ast.literal_eval(value)
        except (ValueError, TypeError):
            return None
    return None


def _down_revision_values(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, tuple | list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _run(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30, check=False)


def _proc_output(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or proc.stderr or "").strip()


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


__all__ = [
    "ReleaseCheckSeverity",
    "ReleaseReadinessCheck",
    "ReleaseReadinessReport",
    "run_release_readiness",
]
