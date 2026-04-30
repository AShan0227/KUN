"""Production preflight checks.

This is deliberately a deployment guard, not a marketing checklist.  It catches
conditions that make a real KUN instance unsafe to expose.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.config import Settings, settings
from kun.engineering.delivery_status import delivery_status_summary, validate_delivery_status

PreflightSeverity = Literal["ok", "warn", "blocker"]


class PreflightCheck(BaseModel):
    """One production readiness check."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    severity: PreflightSeverity
    title: str
    detail: str
    suggested_action: str = ""


class PreflightReport(BaseModel):
    """Aggregated production readiness report."""

    model_config = ConfigDict(extra="forbid")

    env: str
    status: Literal["pass", "warn", "block"]
    checks: list[PreflightCheck] = Field(default_factory=list)
    delivery_summary: dict[str, int] = Field(default_factory=dict)

    @property
    def blockers(self) -> list[PreflightCheck]:
        return [check for check in self.checks if check.severity == "blocker"]

    @property
    def warnings(self) -> list[PreflightCheck]:
        return [check for check in self.checks if check.severity == "warn"]


def run_preflight(
    *,
    cfg: Settings | None = None,
    repo_root: Path | None = None,
    run_alembic_heads: bool = True,
) -> PreflightReport:
    """Run deterministic deployment checks.

    The function is safe for CI and local use: checks that require external
    services are either best-effort or use the existing config validation path.
    """

    active = cfg or settings()
    root = repo_root or Path.cwd()
    checks: list[PreflightCheck] = []
    checks.extend(_config_checks(active))
    checks.extend(_tooling_checks(root, run_alembic_heads=run_alembic_heads))
    checks.extend(_delivery_honesty_checks())

    if any(check.severity == "blocker" for check in checks):
        status: Literal["pass", "warn", "block"] = "block"
    elif any(check.severity == "warn" for check in checks):
        status = "warn"
    else:
        status = "pass"
    return PreflightReport(
        env=active.env,
        status=status,
        checks=checks,
        delivery_summary=delivery_status_summary(),
    )


def _config_checks(cfg: Settings) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    issues = cfg.production_safety_issues()
    if issues:
        checks.append(
            PreflightCheck(
                check_id="production_config",
                severity="blocker" if cfg.env == "production" else "warn",
                title="生产配置不安全",
                detail="；".join(issues),
                suggested_action="修正 env 后再启动 production。",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                check_id="production_config",
                severity="ok",
                title="生产配置基础项通过",
                detail="env/default tenant/auth/db/s3 基础检查通过。",
            )
        )
    if cfg.env != "production":
        checks.append(
            PreflightCheck(
                check_id="env_mode",
                severity="warn",
                title="当前不是 production 模式",
                detail=f"KUN_ENV={cfg.env}，只能说明本地/预发可跑，不能代表正式上线。",
                suggested_action="正式部署前用 KUN_ENV=production 重跑 preflight。",
            )
        )
    return checks


def _tooling_checks(root: Path, *, run_alembic_heads: bool) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    backup_script = root / "scripts" / "backup_postgres.sh"
    restore_script = root / "scripts" / "restore_postgres_smoke.sh"
    for check_id, label, path in (
        ("backup_script", "Postgres 备份脚本", backup_script),
        ("restore_script", "Postgres 恢复 smoke 脚本", restore_script),
    ):
        checks.append(
            PreflightCheck(
                check_id=check_id,
                severity="ok" if path.exists() else "blocker",
                title=f"{label}{'存在' if path.exists() else '缺失'}",
                detail=str(path),
                suggested_action="" if path.exists() else "补齐备份/恢复脚本后再上线。",
            )
        )
    if shutil.which("uv") is None:
        checks.append(
            PreflightCheck(
                check_id="uv_available",
                severity="warn",
                title="uv 不在 PATH",
                detail="无法自动检查 alembic heads。",
                suggested_action="安装 uv 或在 CI 中单独跑 alembic heads。",
            )
        )
        return checks
    if run_alembic_heads:
        checks.append(_alembic_heads_check(root))
    return checks


def _alembic_heads_check(root: Path) -> PreflightCheck:
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        return PreflightCheck(
            check_id="alembic_heads",
            severity="warn",
            title="Alembic head 检查未完成",
            detail="uv 不在 PATH",
            suggested_action="安装 uv 或在 CI 中单独跑 alembic heads。",
        )
    try:
        proc = subprocess.run(
            [uv_bin, "run", "alembic", "heads"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return PreflightCheck(
            check_id="alembic_heads",
            severity="warn",
            title="Alembic head 检查未完成",
            detail=repr(exc),
            suggested_action="在部署环境手动跑 uv run alembic heads。",
        )
    if proc.returncode != 0:
        return PreflightCheck(
            check_id="alembic_heads",
            severity="blocker",
            title="Alembic heads 命令失败",
            detail=(proc.stderr or proc.stdout).strip()[:600],
            suggested_action="修复 migration 后再部署。",
        )
    heads = _non_empty_lines(proc.stdout)
    severity: PreflightSeverity = "ok" if len(heads) == 1 else "blocker"
    return PreflightCheck(
        check_id="alembic_heads",
        severity=severity,
        title="Alembic 单 head" if severity == "ok" else "Alembic 存在多个 head",
        detail="；".join(heads) if heads else "未发现 head 输出",
        suggested_action="" if severity == "ok" else "合并 migration heads 后再部署。",
    )


def _delivery_honesty_checks() -> list[PreflightCheck]:
    issues = validate_delivery_status()
    if not issues:
        return [
            PreflightCheck(
                check_id="delivery_honesty",
                severity="ok",
                title="能力边界标注通过",
                detail="没有发现 ready 但缺证据/缺实现的能力。",
            )
        ]
    return [
        PreflightCheck(
            check_id="delivery_honesty",
            severity="blocker",
            title="能力边界标注不诚实",
            detail="；".join(issues),
            suggested_action="先修正状态或补真实主流程接入，不要带着伪 ready 上线。",
        )
    ]


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def has_blockers(checks: Sequence[PreflightCheck]) -> bool:
    return any(check.severity == "blocker" for check in checks)


__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
    "has_blockers",
    "run_preflight",
]
