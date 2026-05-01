"""V4 release/tag/rollback/hotfix gate.

This is a machine-checkable release companion to the human checklist.  It does
not create a git tag by itself; it tells the operator whether tagging would be
honest and reversible.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
from kun.ops.preflight import run_preflight

ReleaseSeverity = Literal["ok", "warn", "blocker"]


class ReleaseCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    severity: ReleaseSeverity
    title: str
    detail: str
    suggested_action: str = ""


class ReleaseGateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_tag: str
    status: Literal["pass", "warn", "block"]
    checks: list[ReleaseCheck] = Field(default_factory=list)

    @property
    def blockers(self) -> list[ReleaseCheck]:
        return [item for item in self.checks if item.severity == "blocker"]

    @property
    def warnings(self) -> list[ReleaseCheck]:
        return [item for item in self.checks if item.severity == "warn"]


def run_release_gate(
    *,
    release_tag: str,
    repo_root: Path | None = None,
    allow_dirty: bool = False,
    run_git: bool = True,
    run_alembic_heads: bool = True,
    require_ready: bool = False,
) -> ReleaseGateReport:
    """Run V4 release readiness checks."""

    root = repo_root or Path.cwd()
    checks: list[ReleaseCheck] = []
    checks.append(_tag_shape_check(release_tag))
    checks.extend(_checklist_checks(root))
    checks.extend(
        _preflight_checks(
            root,
            run_alembic_heads=run_alembic_heads,
            require_recent_backup_drill=require_ready,
        )
    )
    checks.extend(_delivery_checks(require_ready=require_ready))
    checks.extend(_legal_guard_checks(root))
    if run_git:
        checks.extend(_git_checks(root, release_tag=release_tag, allow_dirty=allow_dirty))

    if any(item.severity == "blocker" for item in checks):
        status: Literal["pass", "warn", "block"] = "block"
    elif any(item.severity == "warn" for item in checks):
        status = "warn"
    else:
        status = "pass"
    return ReleaseGateReport(release_tag=release_tag, status=status, checks=checks)


def _tag_shape_check(release_tag: str) -> ReleaseCheck:
    if re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", release_tag):
        return ReleaseCheck(
            check_id="tag_shape",
            severity="ok",
            title="release tag 格式正确",
            detail=release_tag,
        )
    return ReleaseCheck(
        check_id="tag_shape",
        severity="blocker",
        title="release tag 格式不对",
        detail=release_tag,
        suggested_action="使用 vMAJOR.MINOR.PATCH，例如 v4.0.0。",
    )


def _checklist_checks(root: Path) -> list[ReleaseCheck]:
    path = root / "docs" / "ops" / "release-checklist-v4.md"
    if not path.exists():
        return [
            ReleaseCheck(
                check_id="release_checklist_v4",
                severity="blocker",
                title="缺少 V4 release checklist",
                detail=str(path),
                suggested_action="补 docs/ops/release-checklist-v4.md。",
            )
        ]
    text = path.read_text(encoding="utf-8")
    required = {
        "tag": "tag",
        "rollback": "rollback",
        "hotfix": "hotfix",
        "backup": "backup",
        "restore": "restore",
        "object-store-roundtrip": "object-store-roundtrip",
        "S3/MinIO": "s3",
    }
    lower_text = text.lower()
    missing = [label for label, needle in required.items() if needle not in lower_text]
    if missing:
        return [
            ReleaseCheck(
                check_id="release_checklist_v4",
                severity="blocker",
                title="V4 release checklist 不完整",
                detail="缺少关键词: " + ", ".join(missing),
                suggested_action=(
                    "补齐 tag / rollback / hotfix / backup / restore / "
                    "object-store-roundtrip / S3-MinIO 流程。"
                ),
            )
        ]
    return [
        ReleaseCheck(
            check_id="release_checklist_v4",
            severity="ok",
            title="V4 release checklist 存在且覆盖关键流程",
            detail=str(path),
        )
    ]


def _preflight_checks(
    root: Path,
    *,
    run_alembic_heads: bool,
    require_recent_backup_drill: bool,
) -> list[ReleaseCheck]:
    report = run_preflight(
        repo_root=root,
        run_alembic_heads=run_alembic_heads,
        require_recent_backup_drill=require_recent_backup_drill,
    )
    if report.blockers:
        return [
            ReleaseCheck(
                check_id="preflight",
                severity="blocker",
                title="preflight 仍有 blocker",
                detail=", ".join(item.check_id for item in report.blockers[:8]),
                suggested_action="先修 preflight blocker，再打 tag。",
            )
        ]
    if report.warnings:
        return [
            ReleaseCheck(
                check_id="preflight",
                severity="warn",
                title="preflight 有 warning",
                detail=", ".join(item.check_id for item in report.warnings[:8]),
                suggested_action="确认这些 warning 对当前 release 可接受。",
            )
        ]
    return [
        ReleaseCheck(
            check_id="preflight",
            severity="ok",
            title="preflight 通过",
            detail=report.status,
        )
    ]


def _delivery_checks(*, require_ready: bool) -> list[ReleaseCheck]:
    issues = validate_delivery_status()
    if issues:
        return [
            ReleaseCheck(
                check_id="delivery_honesty",
                severity="blocker",
                title="能力边界标注不诚实",
                detail="; ".join(issues[:5]),
                suggested_action="修 delivery_status，不许把 partial/not_ready 说成完成。",
            )
        ]
    items = get_v3_delivery_status()
    not_ready = [
        item.capability_id for item in items if item.user_visible and item.status != "ready"
    ]
    if not_ready and require_ready:
        return [
            ReleaseCheck(
                check_id="delivery_ready",
                severity="blocker",
                title="仍有用户可见能力不是 ready",
                detail=", ".join(not_ready),
                suggested_action="正式生产 release 前修完，或不要用 --require-ready。",
            )
        ]
    if not_ready:
        return [
            ReleaseCheck(
                check_id="delivery_ready",
                severity="warn",
                title="仍有 partial/not_ready 能力",
                detail=", ".join(not_ready),
                suggested_action="可以做内部/测试版 release，但对外必须继续诚实标注。",
            )
        ]
    return [
        ReleaseCheck(
            check_id="delivery_ready",
            severity="ok",
            title="用户可见能力均 ready",
            detail="delivery-status ready",
        )
    ]


def _legal_guard_checks(root: Path) -> list[ReleaseCheck]:
    script = root / "scripts" / "check_legal_guard.py"
    if not script.exists():
        return [
            ReleaseCheck(
                check_id="legal_guard",
                severity="blocker",
                title="缺少法律/IP guard 脚本",
                detail=str(script),
                suggested_action="补 scripts/check_legal_guard.py。",
            )
        ]
    python_bin = shutil.which("python3") or shutil.which("python")
    if python_bin is None:
        return [
            ReleaseCheck(
                check_id="legal_guard",
                severity="warn",
                title="无法运行法律/IP guard",
                detail="python 不在 PATH",
            )
        ]
    proc = subprocess.run(
        [python_bin, str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        return [
            ReleaseCheck(
                check_id="legal_guard",
                severity="blocker",
                title="法律/IP guard 未通过",
                detail=(proc.stdout + proc.stderr).strip()[:500],
                suggested_action="先补 legal/IP guardrail，再发版。",
            )
        ]
    return [
        ReleaseCheck(
            check_id="legal_guard",
            severity="ok",
            title="法律/IP guard 通过",
            detail="scripts/check_legal_guard.py",
        )
    ]


def _git_checks(root: Path, *, release_tag: str, allow_dirty: bool) -> list[ReleaseCheck]:
    checks: list[ReleaseCheck] = []
    git_bin = shutil.which("git")
    if git_bin is None:
        return [
            ReleaseCheck(
                check_id="git_available",
                severity="warn",
                title="git 不在 PATH",
                detail="无法检查 dirty worktree / tag 是否存在。",
            )
        ]
    status = subprocess.run(
        [git_bin, "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    dirty = bool(status.stdout.strip())
    checks.append(
        ReleaseCheck(
            check_id="git_dirty",
            severity="warn" if allow_dirty and dirty else ("blocker" if dirty else "ok"),
            title="工作区未提交" if dirty else "工作区干净",
            detail=status.stdout.strip()[:500] if dirty else "clean",
            suggested_action="提交或 stash 后再打 tag。" if dirty else "",
        )
    )
    tag = subprocess.run(
        [git_bin, "rev-parse", "-q", "--verify", f"refs/tags/{release_tag}"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    exists = tag.returncode == 0
    checks.append(
        ReleaseCheck(
            check_id="git_tag_available",
            severity="blocker" if exists else "ok",
            title="tag 已存在" if exists else "tag 可创建",
            detail=release_tag,
            suggested_action="换版本号，或确认是否在做 hotfix 重发。" if exists else "",
        )
    )
    return checks


__all__ = ["ReleaseCheck", "ReleaseGateReport", "run_release_gate"]
