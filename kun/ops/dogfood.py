"""V4 dogfood smoke checks.

These checks are intentionally narrow and repeatable.  They prove that the
current product skeleton can be tested safely; they do not claim KUN can already
operate a company end to end.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

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
    include_db_mission: bool = False,
    include_db_account: bool = False,
    include_db_state_ledger_repair: bool = False,
    include_db_long_horizon_drill: bool = False,
) -> DogfoodReport:
    """Run low-risk V4 dogfood checks."""

    scenarios = [
        _scenario_preflight(repo_root=repo_root),
        _scenario_delivery_honesty(),
        _scenario_tenant_token(tenant_id=tenant_id, secret=secret),
        await _scenario_world_gateway_file_write(),
        _scenario_delivery_boundaries_are_visible(),
    ]
    if include_db_mission:
        scenarios.append(await _scenario_mission_resume_db(tenant_id=tenant_id))
    if include_db_account:
        scenarios.append(await _scenario_account_ledger_db(tenant_id=tenant_id, secret=secret))
    if include_db_state_ledger_repair:
        scenarios.append(await _scenario_state_ledger_repair_db(tenant_id=tenant_id))
    if include_db_long_horizon_drill:
        scenarios.append(await _scenario_long_horizon_drill_db(tenant_id=tenant_id))
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


async def _scenario_mission_resume_db(*, tenant_id: str) -> DogfoodScenarioResult:
    """Run one real Mission resume loop against the configured database.

    This is opt-in because it needs the local Postgres/Alembic state.  It is not
    a fake in-memory unit check: it writes a Mission, TASK.md row, RuntimeState,
    asks MissionResumeWorker to claim it, and verifies the durable mission status
    moved to done.
    """

    try:
        from kun.core.db import session_scope
        from kun.core.ids import new_id
        from kun.core.orm import RuntimeStateRow, TaskRow
        from kun.core.tenancy import TenantContext, tenant_scope
        from kun.datamodel.mission import MissionCreate
        from kun.engineering.mission_control import (
            attach_task_to_mission,
            create_mission,
            get_mission,
        )
        from kun.engineering.mission_worker import MissionOrchestratorRunner, MissionResumeWorker
        from kun.engineering.orchestrator import TaskResult
    except Exception as exc:  # pragma: no cover - import failures are deployment issues
        return DogfoodScenarioResult(
            scenario_id="mission_resume_db",
            status="block",
            summary="Mission DB dogfood 依赖导入失败。",
            evidence={"error": f"{type(exc).__name__}: {exc}"},
            next_step="先修 Mission / Orchestrator 依赖导入。",
        )

    task_id = new_id("task")
    try:
        mission = await create_mission(
            MissionCreate(
                title="V4 dogfood long-horizon mission",
                objective="验证 Mission 可以从 queued task 续跑并写回状态。",
                risk_level="low",
                budget_cap_usd=1.0,
                success_metrics=["mission task reaches done"],
            ),
            tenant_id=tenant_id,
            user_id="dogfood-user",
        )
        async with session_scope(tenant_id=tenant_id) as s:
            s.add(
                TaskRow(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    fingerprint=f"dogfood-{task_id}",
                    task_type="dogfood.mission_resume",
                    risk_level="low",
                    complexity_score=0.2,
                    user_id="dogfood-user",
                    estimated_cost_usd=0.05,
                    estimated_duration_sec=5.0,
                    success_criteria_short="Mission resume dogfood reaches done.",
                    spec_json={
                        "goal_detail": "Complete one deterministic Mission resume smoke.",
                        "success_metrics": ["Mission status becomes done"],
                        "constraints": ["No real external dispatch"],
                    },
                )
            )
            s.add(
                RuntimeStateRow(
                    state_id=new_id("runtime"),
                    task_ref=task_id,
                    tenant_id=tenant_id,
                    current_step=0,
                    total_planned_steps=1,
                    status="queued",
                    blob={"dogfood": True},
                )
            )
        await attach_task_to_mission(
            tenant_id=tenant_id,
            mission_id=mission.mission_id,
            task_id=task_id,
            checkpoint={"dogfood": True},
        )

        class FakeMissionOrchestrator:
            async def run_mission_continuation(
                self,
                _request: Any,
                resume_prompt: str,
                *,
                output_kind: str,
            ) -> TaskResult:
                return TaskResult(
                    task_id=new_id("task"),
                    status="done",
                    answer=f"{output_kind}: {resume_prompt[:80]}",
                    cost_usd_equivalent=0.01,
                    tokens_in=10,
                    tokens_out=12,
                    duration_sec=0.1,
                )

        worker = MissionResumeWorker(
            runner=MissionOrchestratorRunner(cast(Any, FakeMissionOrchestrator()))
        )
        with tenant_scope(TenantContext(tenant_id=tenant_id)):
            results = await worker.run_once(tenant_id=tenant_id, limit=5)
        snapshot = await get_mission(tenant_id=tenant_id, mission_id=mission.mission_id)
        completed = [item for item in results if item.status == "completed"]
        ok = bool(completed) and snapshot is not None and snapshot.status == "done"
        return DogfoodScenarioResult(
            scenario_id="mission_resume_db",
            status="pass" if ok else "block",
            summary="Mission 真实 DB 续跑闭环可跑通。" if ok else "Mission 真实 DB 续跑没有完成。",
            evidence={
                "mission_id": mission.mission_id,
                "task_id": task_id,
                "resume_statuses": [item.status for item in results],
                "mission_status": snapshot.status if snapshot else None,
            },
            next_step=""
            if ok
            else "检查 MissionResumeWorker / RuntimeState / Orchestrator runner 接线。",
        )
    except Exception as exc:
        return DogfoodScenarioResult(
            scenario_id="mission_resume_db",
            status="block",
            summary="Mission 真实 DB 续跑 dogfood 无法执行。",
            evidence={"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
            next_step="确认本地 Postgres 已启动、Alembic 已升级、RLS app/admin DSN 配好。",
        )


async def _scenario_long_horizon_drill_db(*, tenant_id: str) -> DogfoodScenarioResult:
    """Run a time-compressed multi-cycle Mission dogfood against the database.

    This is not a claim that KUN has operated a product for a real week.  It is
    a deterministic drill that exercises the same durable path several times:
    queued Mission task -> worker continuation -> runtime outcome -> mission
    review -> story replay.
    """

    try:
        from kun.core.db import session_scope
        from kun.core.ids import new_id
        from kun.core.orm import RuntimeStateRow, TaskRow
        from kun.core.tenancy import TenantContext, tenant_scope
        from kun.datamodel.mission import MissionCreate, MissionNextStep, MissionReview
        from kun.engineering.mission_control import (
            attach_task_to_mission,
            create_mission,
            get_mission,
            get_mission_story,
            record_mission_review,
        )
        from kun.engineering.mission_worker import MissionOrchestratorRunner, MissionResumeWorker
        from kun.engineering.orchestrator import TaskResult
    except Exception as exc:  # pragma: no cover - import failures are deployment issues
        return DogfoodScenarioResult(
            scenario_id="long_horizon_drill_db",
            status="block",
            summary="长期 Mission drill 依赖导入失败。",
            evidence={"error": f"{type(exc).__name__}: {exc}"},
            next_step="先修 Mission / worker / story replay 依赖导入。",
        )

    task_ids = [new_id("task") for _ in range(3)]
    try:
        mission = await create_mission(
            MissionCreate(
                title="V4 dogfood time-compressed product ops",
                objective="用三轮压缩演练验证长期任务可以连续推进、复盘和回放。",
                risk_level="medium",
                budget_cap_usd=1.0,
                success_metrics=[
                    "three mission tasks complete",
                    "mission review history is recorded",
                    "mission story can replay events",
                ],
                strategy={"dogfood_kind": "time_compressed_long_horizon", "logical_days": 7},
            ),
            tenant_id=tenant_id,
            user_id="dogfood-user",
        )
        async with session_scope(tenant_id=tenant_id) as s:
            for idx, task_id in enumerate(task_ids, start=1):
                s.add(
                    TaskRow(
                        task_id=task_id,
                        tenant_id=tenant_id,
                        fingerprint=f"dogfood-long-horizon-{task_id}",
                        task_type=f"dogfood.product_ops.day_{idx}",
                        risk_level="medium",
                        complexity_score=0.45,
                        user_id="dogfood-user",
                        estimated_cost_usd=0.05,
                        estimated_duration_sec=5.0,
                        success_criteria_short=f"Logical day {idx} produces a checkpoint.",
                        spec_json={
                            "goal_detail": f"推进长期运营演练第 {idx} 轮。",
                            "success_metrics": [f"day {idx} checkpoint recorded"],
                            "constraints": [
                                "No real external dispatch",
                                "Keep cost below drill budget",
                            ],
                        },
                    )
                )
                s.add(
                    RuntimeStateRow(
                        state_id=new_id("runtime"),
                        task_ref=task_id,
                        tenant_id=tenant_id,
                        current_step=0,
                        total_planned_steps=1,
                        status="queued",
                        blob={"dogfood": True, "logical_day": idx},
                    )
                )
        for idx, task_id in enumerate(task_ids, start=1):
            await attach_task_to_mission(
                tenant_id=tenant_id,
                mission_id=mission.mission_id,
                task_id=task_id,
                role="daily_ops",
                sequence_no=idx,
                checkpoint={"logical_day": idx, "expected": "checkpoint"},
            )

        class FakeMissionOrchestrator:
            async def run_mission_continuation(
                self,
                request: object,
                resume_prompt: str,
                *,
                output_kind: str,
            ) -> TaskResult:
                task_ref = getattr(request, "task_id", "unknown")
                return TaskResult(
                    task_id=new_id("task"),
                    status="done",
                    answer=f"{output_kind}:{task_ref}:{resume_prompt[:60]}",
                    cost_usd_equivalent=0.03,
                    tokens_in=20,
                    tokens_out=24,
                    duration_sec=0.2,
                )

        worker = MissionResumeWorker(
            runner=MissionOrchestratorRunner(cast(Any, FakeMissionOrchestrator()))
        )
        resume_statuses: list[str] = []
        for idx in range(1, 4):
            with tenant_scope(TenantContext(tenant_id=tenant_id)):
                results = await worker.run_once(tenant_id=tenant_id, limit=1)
            resume_statuses.extend(item.status for item in results)
            await record_mission_review(
                MissionReview(
                    summary=f"第 {idx} 轮演练完成，继续推进下一轮。",
                    budget_notes="成本在 dogfood 预算内。",
                    risk_notes="没有真实外发。",
                    next_step=MissionNextStep(
                        summary=f"推进第 {idx + 1} 轮" if idx < 3 else "整理长期运营复盘",
                        reason="压缩演练需要连续 checkpoint。",
                        task_id=task_ids[idx] if idx < 3 else None,
                        action_type="continue" if idx < 3 else "review",
                    ),
                ),
                tenant_id=tenant_id,
                mission_id=mission.mission_id,
            )

        snapshot = await get_mission(tenant_id=tenant_id, mission_id=mission.mission_id)
        story = await get_mission_story(
            tenant_id=tenant_id,
            mission_id=mission.mission_id,
            history_limit_per_task=50,
        )
        ok = (
            snapshot is not None
            and snapshot.status == "done"
            and resume_statuses.count("completed") == 3
            and story is not None
            and story.task_count == 3
            and story.event_count >= 3
            and story.total_event_cost_usd <= 1.0
        )
        return DogfoodScenarioResult(
            scenario_id="long_horizon_drill_db",
            status="pass" if ok else "block",
            summary="时间压缩长期 Mission drill 可连续推进、复盘并回放故事线。"
            if ok
            else "时间压缩长期 Mission drill 没有跑通。",
            evidence={
                "mission_id": mission.mission_id,
                "task_ids": task_ids,
                "resume_statuses": resume_statuses,
                "mission_status": snapshot.status if snapshot else None,
                "story_task_count": story.task_count if story else None,
                "story_event_count": story.event_count if story else None,
                "story_cost_usd": story.total_event_cost_usd if story else None,
                "honest_limit": "time-compressed drill; not a real cross-week production run",
            },
            next_step="" if ok else "检查 Mission worker 多轮 claim、review 写回和 story replay。",
        )
    except Exception as exc:
        return DogfoodScenarioResult(
            scenario_id="long_horizon_drill_db",
            status="block",
            summary="长期 Mission drill 无法执行。",
            evidence={"task_ids": task_ids, "error": f"{type(exc).__name__}: {exc}"},
            next_step="确认本地 Postgres 已启动、Alembic 已升级、RLS app/admin DSN 配好。",
        )


async def _scenario_account_ledger_db(*, tenant_id: str, secret: str) -> DogfoodScenarioResult:
    """Run one account ledger + session + invite smoke against the configured DB."""

    try:
        from kun.core.db import session_scope
        from kun.ops.account_registry import (
            accept_tenant_member_invite,
            hash_bearer_token,
            invite_tenant_member,
            record_token_usage,
            upsert_tenant_account_member,
        )
        from kun.ops.account_sessions import issue_session_token_pair, refresh_session_access_token
    except Exception as exc:  # pragma: no cover - import failures are deployment issues
        return DogfoodScenarioResult(
            scenario_id="account_ledger_db",
            status="block",
            summary="账号账本 DB dogfood 依赖导入失败。",
            evidence={"error": f"{type(exc).__name__}: {exc}"},
            next_step="先修账号账本 / session 依赖导入。",
        )

    owner_user_id = "dogfood-owner"
    invited_user_id = "dogfood-invited"
    try:
        async with session_scope(tenant_id=tenant_id) as s:
            account = await upsert_tenant_account_member(
                s,
                tenant_id=tenant_id,
                organization_id=f"{tenant_id}-org",
                display_name=f"{tenant_id} dogfood",
                owner_user_id=owner_user_id,
                scopes=["account:read", "account:admin", "chat:write"],
                role="owner",
                plan="dev",
                billing_status="manual",
                metadata={"source": "ops.dogfood.account_ledger"},
            )
            pair = await issue_session_token_pair(
                s,
                tenant_id=tenant_id,
                user_id=owner_user_id,
                secret=secret,
                scopes=["account:read", "account:admin", "chat:write"],
                audience="developer",
                metadata={"source": "ops.dogfood.account_ledger"},
            )
            usage_recorded = await record_token_usage(
                s,
                tenant_id=tenant_id,
                token_hash=hash_bearer_token(pair.access_token),
                ip_hash="dogfood-ip-hash",
                user_agent="kun-dogfood",
            )
            refreshed = await refresh_session_access_token(
                s,
                refresh_token=pair.refresh_token,
                auth_secrets=[secret],
                signing_secret=secret,
                access_ttl_sec=300,
            )
            invited = await invite_tenant_member(
                s,
                tenant_id=tenant_id,
                user_id=invited_user_id,
                role="viewer",
                scopes=["account:read"],
                invite_secret=secret,
                invited_by_user_id=owner_user_id,
            )
            accepted = await accept_tenant_member_invite(
                s,
                tenant_id=tenant_id,
                user_id=invited_user_id,
                invite_token=invited.acceptance_token,
                auth_secrets=[secret],
            )
            invited_pair = await issue_session_token_pair(
                s,
                tenant_id=tenant_id,
                user_id=invited_user_id,
                secret=secret,
                scopes=accepted.scopes,
                audience="developer",
                metadata={"source": "ops.dogfood.account_invite_accept"},
            )
        ok = (
            account.persisted
            and usage_recorded
            and pair.refresh_token_id == refreshed.refresh_token_id
            and invited.status in {"invited", "active"}
            and accepted.status == "active"
            and bool(invited_pair.refresh_token_id)
        )
        return DogfoodScenarioResult(
            scenario_id="account_ledger_db",
            status="pass" if ok else "block",
            summary="账号账本、token 使用账本、refresh session、成员邀请和接受邀请 DB smoke 可跑通。"
            if ok
            else "账号账本 DB smoke 没有跑通。",
            evidence={
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "access_token_id": pair.access_token_id,
                "usage_recorded": usage_recorded,
                "refresh_token_id": pair.refresh_token_id,
                "refreshed_access_token_id": refreshed.access_token_id,
                "invited_user_id": invited.user_id,
                "invite_status": invited.status,
                "acceptance_token_id": invited.acceptance_token_id,
                "accepted_status": accepted.status,
                "accepted_role": accepted.role,
                "invited_access_token_id": invited_pair.access_token_id,
                "invited_refresh_token_id": invited_pair.refresh_token_id,
            },
            next_step="" if ok else "检查 tenant account / session / invite accept 写库链路。",
        )
    except Exception as exc:
        return DogfoodScenarioResult(
            scenario_id="account_ledger_db",
            status="block",
            summary="账号账本 DB dogfood 无法执行。",
            evidence={"tenant_id": tenant_id, "error": f"{type(exc).__name__}: {exc}"},
            next_step="确认本地 Postgres 已启动、Alembic 已升级、RLS app/admin DSN 配好。",
        )


async def _scenario_state_ledger_repair_db(*, tenant_id: str) -> DogfoodScenarioResult:
    """Run one StateLedger repair smoke against the configured DB."""

    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.ids import new_id
        from kun.core.orm import EventRow, StateLedgerEntryRow, TaskRow
        from kun.ops.state_ledger_repair import repair_state_ledger_snapshot
    except Exception as exc:  # pragma: no cover - import failures are deployment issues
        return DogfoodScenarioResult(
            scenario_id="state_ledger_repair_db",
            status="block",
            summary="StateLedger repair DB dogfood 依赖导入失败。",
            evidence={"error": f"{type(exc).__name__}: {exc}"},
            next_step="先修 StateLedger / EventRow / repair 依赖导入。",
        )

    task_id = new_id("task")
    try:
        async with session_scope(tenant_id=tenant_id) as s:
            s.add(
                TaskRow(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    fingerprint=f"dogfood-ledger-repair-{task_id}",
                    task_type="dogfood.state_ledger_repair",
                    risk_level="low",
                    complexity_score=0.2,
                    user_id="dogfood-user",
                    estimated_cost_usd=0.05,
                    estimated_duration_sec=5.0,
                    success_criteria_short="StateLedger repair dogfood reaches done.",
                    spec_json={"goal_detail": "Verify EventRow can repair current ledger."},
                )
            )
            s.add(
                EventRow(
                    event_id=new_id("event"),
                    tenant_id=tenant_id,
                    event_type="task.created",
                    subject="StateLedger repair dogfood task created",
                    payload={"reason": "dogfood created"},
                    task_ref=task_id,
                )
            )
            s.add(
                EventRow(
                    event_id=new_id("event"),
                    tenant_id=tenant_id,
                    event_type="task.done",
                    subject="StateLedger repair dogfood task done",
                    payload={"status": "done", "cost_delta_usd": 0.02},
                    task_ref=task_id,
                )
            )
            s.add(
                StateLedgerEntryRow(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    user_id="dogfood-user",
                    status="running",
                    snapshot_json={
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "user_id": "dogfood-user",
                        "status": "running",
                        "current_action": "stale snapshot",
                        "cost_so_far_usd": 0.0,
                    },
                )
            )

        result = await repair_state_ledger_snapshot(
            tenant_id=tenant_id,
            task_id=task_id,
            user_id="dogfood-user",
            apply=True,
        )
        async with session_scope(tenant_id=tenant_id) as s:
            repaired = (
                await s.execute(
                    select(StateLedgerEntryRow).where(
                        StateLedgerEntryRow.tenant_id == tenant_id,
                        StateLedgerEntryRow.task_id == task_id,
                    )
                )
            ).scalar_one_or_none()
        status = repaired.status if repaired is not None else "missing"
        snapshot = repaired.snapshot_json if repaired is not None else {}
        ok = result.applied and status == "done" and snapshot.get("cost_so_far_usd") == 0.02
        return DogfoodScenarioResult(
            scenario_id="state_ledger_repair_db",
            status="pass" if ok else "block",
            summary="StateLedger repair 可从 EventRow 回放修复当前快照。"
            if ok
            else "StateLedger repair 没能修复当前快照。",
            evidence={
                "task_id": task_id,
                "applied": result.applied,
                "diff_count": len(result.diffs),
                "event_count": result.event_count,
                "status_after": status,
                "cost_after": snapshot.get("cost_so_far_usd"),
            },
            next_step="" if ok else "检查 EventRow 回放、StateLedger repair 和 RLS 写回路径。",
        )
    except Exception as exc:
        return DogfoodScenarioResult(
            scenario_id="state_ledger_repair_db",
            status="block",
            summary="StateLedger repair DB dogfood 无法执行。",
            evidence={"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
            next_step="确认本地 Postgres 已启动、Alembic 已升级、RLS app/admin DSN 配好。",
        )


__all__ = [
    "DogfoodReport",
    "DogfoodScenarioResult",
    "DogfoodStatus",
    "run_v4_dogfood",
]
