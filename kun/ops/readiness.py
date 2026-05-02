"""One-command readiness report for formal KUN testing.

This aggregates existing honest gates instead of inventing another promise
layer: preflight, delivery status, secret audit, and optional dogfood.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.delivery_status import (
    DeliveryCapability,
    delivery_status_summary,
    get_v3_delivery_status,
    validate_delivery_status,
)
from kun.ops.dogfood import DogfoodReport, run_v4_dogfood
from kun.ops.preflight import PreflightReport, run_preflight
from kun.ops.secret_audit import SecretAuditReport, audit_runtime_secrets


class BackupReadinessSummary(BaseModel):
    """Human-facing summary for the backup/restore drill gate."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "warn", "block", "unknown"] = "unknown"
    check_id: str = "backup_drill_freshness"
    title: str = "备份恢复演练状态未知"
    detail: str = ""
    suggested_action: str = ""
    required: bool = False
    max_age_hours: float = 168.0


class ReadinessReport(BaseModel):
    """Aggregated readiness report for human + CI use."""

    model_config = ConfigDict(extra="forbid")

    status: str
    tenant_id: str
    preflight: PreflightReport
    secret_audit: SecretAuditReport
    delivery_summary: dict[str, int] = Field(default_factory=dict)
    delivery_issues: list[str] = Field(default_factory=list)
    backup_drill: BackupReadinessSummary = Field(default_factory=BackupReadinessSummary)
    dogfood: DogfoodReport | None = None
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


async def run_readiness_report(
    *,
    tenant_id: str = "u-sylvan",
    include_dogfood: bool = False,
    include_db_mission: bool = False,
    include_db_account: bool = False,
    include_db_state_ledger_repair: bool = False,
    include_db_long_horizon_drill: bool = False,
    run_alembic_heads: bool = True,
    require_recent_backup_drill: bool = False,
    backup_drill_max_age_hours: float = 168.0,
) -> ReadinessReport:
    """Run the current readiness gates and return one compact report."""

    preflight = run_preflight(
        run_alembic_heads=run_alembic_heads,
        require_recent_backup_drill=require_recent_backup_drill,
        backup_drill_max_age_hours=backup_drill_max_age_hours,
    )
    secret_audit = audit_runtime_secrets()
    delivery_items = get_v3_delivery_status()
    delivery_issues = validate_delivery_status(delivery_items)
    dogfood: DogfoodReport | None = None
    if include_dogfood:
        dogfood = await run_v4_dogfood(
            tenant_id=tenant_id,
            include_db_mission=include_db_mission,
            include_db_account=include_db_account,
            include_db_state_ledger_repair=include_db_state_ledger_repair,
            include_db_long_horizon_drill=include_db_long_horizon_drill,
        )
    backup_drill = _backup_readiness_summary(
        preflight,
        required=require_recent_backup_drill,
        max_age_hours=backup_drill_max_age_hours,
    )

    blockers = _blockers(
        preflight=preflight,
        secret_audit=secret_audit,
        delivery_issues=delivery_issues,
        dogfood=dogfood,
    )
    warnings = _warnings(
        preflight=preflight,
        secret_audit=secret_audit,
        delivery_items=delivery_items,
        dogfood=dogfood,
    )
    status = "block" if blockers else ("warn" if warnings else "pass")
    return ReadinessReport(
        status=status,
        tenant_id=tenant_id,
        preflight=preflight,
        secret_audit=secret_audit,
        delivery_summary=_delivery_summary(delivery_items),
        delivery_issues=delivery_issues,
        backup_drill=backup_drill,
        dogfood=dogfood,
        blockers=blockers,
        warnings=warnings,
        next_steps=_next_steps(
            blockers=blockers,
            warnings=warnings,
            dogfood=dogfood,
            backup_drill=backup_drill,
        ),
    )


def _backup_readiness_summary(
    preflight: PreflightReport,
    *,
    required: bool,
    max_age_hours: float,
) -> BackupReadinessSummary:
    check = next(
        (item for item in preflight.checks if item.check_id == "backup_drill_freshness"),
        None,
    )
    if check is None:
        return BackupReadinessSummary(
            status="unknown",
            detail="preflight 没有返回 backup_drill_freshness 检查。",
            suggested_action="确认 preflight 工具链没有被裁剪。",
            required=required,
            max_age_hours=max_age_hours,
        )
    status_by_severity: dict[str, Literal["pass", "warn", "block", "unknown"]] = {
        "ok": "pass",
        "warn": "warn",
        "blocker": "block",
    }
    return BackupReadinessSummary(
        status=status_by_severity.get(check.severity, "unknown"),
        check_id=check.check_id,
        title=check.title,
        detail=check.detail,
        suggested_action=check.suggested_action,
        required=required,
        max_age_hours=max_age_hours,
    )


def _blockers(
    *,
    preflight: PreflightReport,
    secret_audit: SecretAuditReport,
    delivery_issues: list[str],
    dogfood: DogfoodReport | None,
) -> list[str]:
    blockers: list[str] = []
    blockers.extend(f"preflight:{item.check_id}" for item in preflight.blockers)
    blockers.extend(f"secret_audit:{item.item_id}" for item in secret_audit.blockers)
    blockers.extend(f"delivery:{issue}" for issue in delivery_issues)
    if dogfood is not None:
        blockers.extend(f"dogfood:{item.scenario_id}" for item in dogfood.blockers)
    return blockers


def _warnings(
    *,
    preflight: PreflightReport,
    secret_audit: SecretAuditReport,
    delivery_items: list[DeliveryCapability],
    dogfood: DogfoodReport | None,
) -> list[str]:
    warnings: list[str] = []
    warnings.extend(f"preflight:{item.check_id}" for item in preflight.warnings)
    warnings.extend(f"secret_audit:{item.item_id}" for item in secret_audit.warnings)
    partial_or_missing = [
        str(getattr(item, "capability_id", "unknown"))
        for item in delivery_items
        if str(getattr(item, "status", "")) != "ready"
    ]
    warnings.extend(f"delivery_partial:{item}" for item in partial_or_missing)
    if dogfood is not None and dogfood.status == "warn":
        warnings.extend(f"dogfood_warn:{item.scenario_id}" for item in dogfood.scenarios)
    return warnings


def _delivery_summary(items: list[DeliveryCapability]) -> dict[str, int]:
    counts = delivery_status_summary()
    return {key: sum(1 for item in items if item.status == key) for key in counts}


def _next_steps(
    *,
    blockers: list[str],
    warnings: list[str],
    dogfood: DogfoodReport | None,
    backup_drill: BackupReadinessSummary,
) -> list[str]:
    if blockers:
        steps = [
            "先修 blocker，不要进入正式测试。",
            "优先看 preflight / secret_audit / dogfood 的 blocker 明细。",
        ]
        if backup_drill.status == "block":
            steps.append("先完成最近一次备份恢复演练；恢复能力没验证前，不要做生产发布。")
        return steps
    steps = ["可以进入人工 dogfood，但必须带着 warnings 清单测试。"]
    if backup_drill.status in {"warn", "unknown"}:
        steps.append("建议先跑一次备份恢复演练，再进入更长周期 dogfood。")
    if dogfood is None:
        steps.append(
            "如需更真实验收，加 --include-dogfood；本地有数据库再加 --include-db-mission / --include-db-account。"
        )
    if warnings:
        steps.append("不要把 partial/not_ready 能力对用户说成已完成。")
    return steps


__all__ = ["BackupReadinessSummary", "ReadinessReport", "run_readiness_report"]
