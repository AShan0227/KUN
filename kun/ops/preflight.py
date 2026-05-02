"""Production preflight checks.

This is deliberately a deployment guard, not a marketing checklist.  It catches
conditions that make a real KUN instance unsafe to expose.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from os import X_OK, access, environ
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.config import Settings, settings
from kun.engineering.delivery_status import delivery_status_summary, validate_delivery_status
from kun.ops.backup_restore import check_backup_drill_freshness
from kun.ops.secret_audit import audit_runtime_secrets
from kun.world.handler_health import EXPECTED_REAL_WORLD_HANDLERS
from kun.world.tenant_env import env_for_tenant, has_any_scoped_env, missing_required_world_env

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
    require_recent_backup_drill: bool | None = None,
    backup_drill_max_age_hours: float = 168.0,
) -> PreflightReport:
    """Run deterministic deployment checks.

    The function is safe for CI and local use: checks that require external
    services are either best-effort or use the existing config validation path.
    """

    active = cfg or settings()
    root = repo_root or Path.cwd()
    checks: list[PreflightCheck] = []
    checks.extend(_config_checks(active))
    checks.extend(_secret_audit_checks(active))
    checks.extend(_world_gateway_config_checks())
    checks.extend(
        _tooling_checks(
            root,
            run_alembic_heads=run_alembic_heads,
            require_recent_backup_drill=_require_recent_backup_drill(require_recent_backup_drill),
            backup_drill_max_age_hours=backup_drill_max_age_hours,
        )
    )
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


def _secret_audit_checks(cfg: Settings) -> list[PreflightCheck]:
    """Expose NUO's deeper secret/config audit in the release preflight."""

    report = audit_runtime_secrets(cfg=cfg)
    checks: list[PreflightCheck] = []
    for item in report.items:
        # Keep the legacy production_config check as the short summary, and
        # add the detailed audit rows under a dedicated namespace.
        checks.append(
            PreflightCheck(
                check_id=f"secret_audit:{item.item_id}",
                severity=item.severity,
                title=item.title,
                detail=item.detail,
                suggested_action=item.suggested_action,
            )
        )
    return checks


def _tooling_checks(
    root: Path,
    *,
    run_alembic_heads: bool,
    require_recent_backup_drill: bool,
    backup_drill_max_age_hours: float,
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    backup_script = root / "scripts" / "backup_postgres.sh"
    restore_script = root / "scripts" / "restore_postgres_smoke.sh"
    backup_drill_script = root / "scripts" / "backup_restore_drill.py"
    for check_id, label, path in (
        ("backup_script", "Postgres 备份脚本", backup_script),
        ("restore_script", "Postgres 恢复 smoke 脚本", restore_script),
    ):
        checks.append(_script_exists_and_executable_check(check_id, label, path))
    checks.extend(_backup_tool_checks())
    checks.append(
        PreflightCheck(
            check_id="backup_restore_drill_script",
            severity="ok" if backup_drill_script.exists() else "warn",
            title=f"本地备份恢复演练脚本{'存在' if backup_drill_script.exists() else '缺失'}",
            detail=str(backup_drill_script),
            suggested_action=""
            if backup_drill_script.exists()
            else "补齐演练脚本，至少让本地配置备份可校验。",
        )
    )
    checks.append(
        _backup_drill_freshness_check(
            root,
            require_recent=require_recent_backup_drill,
            max_age_hours=backup_drill_max_age_hours,
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


def _script_exists_and_executable_check(
    check_id: str,
    label: str,
    path: Path,
) -> PreflightCheck:
    if not path.exists():
        return PreflightCheck(
            check_id=check_id,
            severity="blocker",
            title=f"{label}缺失",
            detail=str(path),
            suggested_action="补齐备份/恢复脚本后再上线。",
        )
    if not access(path, X_OK):
        return PreflightCheck(
            check_id=check_id,
            severity="blocker",
            title=f"{label}不可执行",
            detail=str(path),
            suggested_action=f"运行 chmod +x {path}，并在目标环境重跑 preflight。",
        )
    return PreflightCheck(
        check_id=check_id,
        severity="ok",
        title=f"{label}存在且可执行",
        detail=str(path),
    )


def _backup_tool_checks() -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    for tool in ("pg_dump", "pg_restore", "psql"):
        tool_path = shutil.which(tool)
        checks.append(
            PreflightCheck(
                check_id=f"backup_tool:{tool}",
                severity="ok" if tool_path else "warn",
                title=f"{tool} {'可用' if tool_path else '不在 PATH'}",
                detail=tool_path or "missing",
                suggested_action="" if tool_path else f"在备份/恢复运行环境安装 {tool}。",
            )
        )
    checksum_tool = shutil.which("sha256sum") or shutil.which("shasum")
    checks.append(
        PreflightCheck(
            check_id="backup_tool:checksum",
            severity="ok" if checksum_tool else "warn",
            title=f"checksum 工具{'可用' if checksum_tool else '不在 PATH'}",
            detail=checksum_tool or "missing",
            suggested_action="" if checksum_tool else "安装 sha256sum 或 shasum。",
        )
    )
    return checks


def _backup_drill_freshness_check(
    root: Path,
    *,
    require_recent: bool,
    max_age_hours: float,
) -> PreflightCheck:
    report = check_backup_drill_freshness(
        backup_dir=root / "backups",
        max_age_hours=max_age_hours,
        require_recent=require_recent,
    )
    if report.status == "pass":
        return PreflightCheck(
            check_id="backup_drill_freshness",
            severity="ok",
            title="最近备份演练存在",
            detail=(
                f"{report.latest_manifest_path}；age={report.age_hours}h；"
                f"max={report.max_age_hours}h"
            ),
        )
    return PreflightCheck(
        check_id="backup_drill_freshness",
        severity="blocker" if report.status == "block" else "warn",
        title="最近备份演练缺失或过期",
        detail="；".join(report.notes) or f"backup_dir={report.backup_dir}",
        suggested_action=(
            "运行 uv run kun ops backup-drill-create --output-dir backups，"
            "再跑 backup-drill-restore-dry-run；正式发布前建议再跑 object-store-roundtrip。"
        ),
    )


def _world_gateway_config_checks() -> list[PreflightCheck]:
    """Catch half-enabled real-world handlers before startup.

    Disabled real handlers are allowed because not every tenant needs email /
    browser / enterprise API on day one.  But once an operator sets an enable
    flag, missing required env means the gateway would fail at runtime, so it is
    a deployment blocker.
    """

    checks: list[PreflightCheck] = []
    for action_type, (enable_env, required_envs) in EXPECTED_REAL_WORLD_HANDLERS.items():
        enabled = _env_truthy(env_for_tenant("", enable_env))
        missing = missing_required_world_env(required_envs)
        if enabled and missing:
            checks.append(
                PreflightCheck(
                    check_id=f"world_handler_config:{action_type}",
                    severity="blocker",
                    title=f"WorldGateway {action_type} 配置不完整",
                    detail=f"{enable_env}=true，但缺少 {', '.join(missing)}",
                    suggested_action="补齐 env，或关闭对应 KUN_WORLD_*_ENABLED 开关。",
                )
            )
        elif enabled:
            checks.append(
                PreflightCheck(
                    check_id=f"world_handler_config:{action_type}",
                    severity="ok",
                    title=f"WorldGateway {action_type} 基础配置通过",
                    detail=(
                        f"{enable_env}=true，必需 env 已提供。"
                        " 可用全局 env，也可用 KUN_TENANT_<TENANT>_* 租户级 env。"
                    ),
                )
            )
        else:
            configured_envs = _configured_world_envs(required_envs)
            if configured_envs:
                checks.append(
                    PreflightCheck(
                        check_id=f"world_handler_config:{action_type}",
                        severity="warn",
                        title=f"WorldGateway {action_type} 密钥已配置但未启用",
                        detail=(
                            f"检测到 {', '.join(configured_envs)}，"
                            f"但 {enable_env} 未开启；这个真实外部通道不会注册。"
                        ),
                        suggested_action=(
                            f"如果要真实启用，设置 {enable_env}=true 并跑 dogfood；"
                            "如果暂时不用，移除这些密钥以减少误解和泄漏面。"
                        ),
                    )
                )
    return checks


def _configured_world_envs(required_envs: tuple[str, ...]) -> list[str]:
    configured: list[str] = []
    for name in required_envs:
        if env_for_tenant("", name) is not None or has_any_scoped_env(name):
            configured.append(name)
    return configured


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


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_recent_backup_drill(value: bool | None) -> bool:
    if value is not None:
        return value
    return _env_truthy(environ.get("KUN_REQUIRE_RECENT_BACKUP_DRILL"))


def has_blockers(checks: Sequence[PreflightCheck]) -> bool:
    return any(check.severity == "blocker" for check in checks)


__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
    "has_blockers",
    "run_preflight",
]
