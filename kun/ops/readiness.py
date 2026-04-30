"""One-command readiness report for formal KUN testing.

This aggregates existing honest gates instead of inventing another promise
layer: preflight, delivery status, secret audit, and optional dogfood.
"""

from __future__ import annotations

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


class ReadinessReport(BaseModel):
    """Aggregated readiness report for human + CI use."""

    model_config = ConfigDict(extra="forbid")

    status: str
    tenant_id: str
    preflight: PreflightReport
    secret_audit: SecretAuditReport
    delivery_summary: dict[str, int] = Field(default_factory=dict)
    delivery_issues: list[str] = Field(default_factory=list)
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
    run_alembic_heads: bool = True,
) -> ReadinessReport:
    """Run the current readiness gates and return one compact report."""

    preflight = run_preflight(run_alembic_heads=run_alembic_heads)
    secret_audit = audit_runtime_secrets()
    delivery_items = get_v3_delivery_status()
    delivery_issues = validate_delivery_status(delivery_items)
    dogfood: DogfoodReport | None = None
    if include_dogfood:
        dogfood = await run_v4_dogfood(
            tenant_id=tenant_id,
            include_db_mission=include_db_mission,
            include_db_account=include_db_account,
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
        dogfood=dogfood,
        blockers=blockers,
        warnings=warnings,
        next_steps=_next_steps(blockers=blockers, warnings=warnings, dogfood=dogfood),
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
) -> list[str]:
    if blockers:
        return [
            "先修 blocker，不要进入正式测试。",
            "优先看 preflight / secret_audit / dogfood 的 blocker 明细。",
        ]
    steps = ["可以进入人工 dogfood，但必须带着 warnings 清单测试。"]
    if dogfood is None:
        steps.append(
            "如需更真实验收，加 --include-dogfood；本地有数据库再加 --include-db-mission / --include-db-account。"
        )
    if warnings:
        steps.append("不要把 partial/not_ready 能力对用户说成已完成。")
    return steps


__all__ = ["ReadinessReport", "run_readiness_report"]
