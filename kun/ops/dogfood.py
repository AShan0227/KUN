"""V4 dogfood smoke checks.

These checks are intentionally narrow and repeatable.  They prove that the
current product skeleton can be tested safely; they do not claim KUN can already
operate a company end to end.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
from kun.interface.hermes import NoopHermesAdapter
from kun.ops.preflight import run_preflight
from kun.ops.tenant_onboarding import create_tenant_onboarding_pack
from kun.security.auth import verify_bearer_token
from kun.world.gateway import WorldAction, WorldGateway

DogfoodStatus = Literal["pass", "warn", "block"]


class DogfoodScenarioResult(BaseModel):
    """One V4 dogfood scenario result."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    status: DogfoodStatus
    summary: str
    evidence: dict[str, object] = Field(default_factory=dict)
    next_step: str = ""


class DogfoodReport(BaseModel):
    """V4 dogfood smoke report."""

    model_config = ConfigDict(extra="forbid")

    status: DogfoodStatus
    scenarios: list[DogfoodScenarioResult] = Field(default_factory=list)

    @property
    def blockers(self) -> list[DogfoodScenarioResult]:
        return [item for item in self.scenarios if item.status == "block"]

    @property
    def warnings(self) -> list[DogfoodScenarioResult]:
        return [item for item in self.scenarios if item.status == "warn"]


async def run_v4_dogfood(
    *,
    tenant_id: str = "u-sylvan",
    repo_root: Path | None = None,
    secret: str = "dogfood-secret-" + "x" * 32,
) -> DogfoodReport:
    """Run low-risk V4 dogfood checks."""

    scenarios = [
        _scenario_preflight(repo_root=repo_root),
        _scenario_delivery_honesty(),
        _scenario_tenant_token(tenant_id=tenant_id, secret=secret),
        await _scenario_world_gateway_file_write(),
        _scenario_delivery_boundaries_are_visible(),
    ]
    if any(item.status == "block" for item in scenarios):
        status: DogfoodStatus = "block"
    elif any(item.status == "warn" for item in scenarios):
        status = "warn"
    else:
        status = "pass"
    return DogfoodReport(status=status, scenarios=scenarios)


def _scenario_preflight(repo_root: Path | None) -> DogfoodScenarioResult:
    report = run_preflight(repo_root=repo_root, run_alembic_heads=False)
    if report.blockers:
        return DogfoodScenarioResult(
            scenario_id="production_preflight",
            status="block",
            summary="上线前硬检查存在 blocker。",
            evidence={"blockers": [item.title for item in report.blockers]},
            next_step="先修 blocker，再跑 dogfood。",
        )
    return DogfoodScenarioResult(
        scenario_id="production_preflight",
        status="warn" if report.warnings else "pass",
        summary="上线前硬检查可运行；当前环境可能仍是 dev/warn。",
        evidence={"status": report.status, "warnings": [item.title for item in report.warnings]},
    )


def _scenario_delivery_honesty() -> DogfoodScenarioResult:
    issues = validate_delivery_status()
    return DogfoodScenarioResult(
        scenario_id="delivery_honesty",
        status="block" if issues else "pass",
        summary="能力边界标注没有把 partial/not_ready 冒充 ready。"
        if not issues
        else "能力边界标注不诚实。",
        evidence={"issues": issues},
        next_step="修 delivery_status 状态或补真实主流程接入。" if issues else "",
    )


def _scenario_tenant_token(*, tenant_id: str, secret: str) -> DogfoodScenarioResult:
    pack = create_tenant_onboarding_pack(
        tenant_id=tenant_id,
        user_id="dogfood-user",
        scopes=["world:approve", "world:dispatch"],
        secret=secret,
    )
    claims = verify_bearer_token(f"Bearer {pack.bearer_token}", secret)
    ok = claims.tenant_id == tenant_id and "world:approve" in claims.scopes
    return DogfoodScenarioResult(
        scenario_id="tenant_onboarding_token",
        status="pass" if ok else "block",
        summary="租户启动 token 可验签并携带权限。" if ok else "租户启动 token 验证失败。",
        evidence={
            "tenant_id": claims.tenant_id,
            "scopes": list(claims.scopes),
            "missing_full_product": pack.missing_full_product,
        },
        next_step="" if ok else "检查 KUN_AUTH_SECRET 和 token 生成逻辑。",
    )


async def _scenario_world_gateway_file_write() -> DogfoodScenarioResult:
    with tempfile.TemporaryDirectory(prefix="kun-v4-dogfood-") as tmp:
        gateway = WorldGateway(artifact_root=tmp, hermes_adapter=NoopHermesAdapter())
        action = WorldAction(
            action_id="act-dogfood-file",
            task_ref="task-dogfood",
            action_type="local_file.write",
            target_ref="notes/result.md",
            risk_level="low",
            payload={"content": "dogfood ok\n"},
        )
        preview = await gateway.preview(action)
        result = await gateway.execute_approved(action)
        written = Path(tmp, "files", "notes", "result.md")
        artifact_exists = await asyncio.to_thread(written.exists)
        artifact_text = (
            await asyncio.to_thread(written.read_text, encoding="utf-8") if artifact_exists else ""
        )
        ok = (
            preview.gateway_mode == "handler_preview"
            and result.external_dispatched
            and artifact_exists
            and artifact_text == "dogfood ok\n"
        )
        return DogfoodScenarioResult(
            scenario_id="world_gateway_low_risk_handler",
            status="pass" if ok else "block",
            summary="WorldGateway 低风险本地文件 handler 预览和执行都可跑。"
            if ok
            else "WorldGateway 低风险 handler 没跑通。",
            evidence={
                "preview_mode": preview.gateway_mode,
                "execute_mode": result.gateway_mode,
                "artifact_exists": artifact_exists,
            },
            next_step="" if ok else "修 local_file.write handler 或审批执行链。",
        )


def _scenario_delivery_boundaries_are_visible() -> DogfoodScenarioResult:
    items = {item.capability_id: item for item in get_v3_delivery_status()}
    production = items.get("production_deployment")
    long_horizon = items.get("long_horizon_tasks")
    ok = (
        production is not None
        and production.status == "not_ready"
        and long_horizon is not None
        and long_horizon.status == "partial"
    )
    return DogfoodScenarioResult(
        scenario_id="honest_product_boundaries",
        status="pass" if ok else "block",
        summary="关键产品边界可见：生产级仍 not_ready，长周期任务仍 partial。"
        if ok
        else "关键产品边界被误标，可能误导测试使用。",
        evidence={
            "production_deployment": production.status if production else None,
            "long_horizon_tasks": long_horizon.status if long_horizon else None,
        },
        next_step="" if ok else "恢复 delivery_status 的诚实状态。",
    )


__all__ = [
    "DogfoodReport",
    "DogfoodScenarioResult",
    "DogfoodStatus",
    "run_v4_dogfood",
]
