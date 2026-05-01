"""Blackboard MVP — 黑板 (V2.1 §2.3 + §9.3 / T16).

5 endpoint:
- GET /api/blackboard/tasks       任务看板
- GET /api/blackboard/events      事件流
- GET /api/blackboard/state       全局状态区 (预算 / 安全等级 / 系统压力)
- GET /api/blackboard/workspace/{task_id}   共享工作区
- GET /api/blackboard/assets/{task_id}      资产池活跃切片

双重渲染 (V2.1 §2.3):
- 对人 UI: 精简 5 个核心信息块
- 对 agent: 完整 JSON dump (按 §16.7 LayeredAsset L1/L2/L3)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from kun.core.config import settings
from kun.core.state_ledger import StateLedgerEntry, replay_state_ledger_story
from kun.core.tenancy import current_tenant

router = APIRouter(prefix="/api/blackboard", tags=["blackboard"])


# ---- 视图层 (从底层数据派生, 不存储) ----


class TaskBoardItem(BaseModel):
    task_id: str
    title: str = ""
    status: Literal["queued", "running", "paused", "done", "failed", "cancelled"] = "queued"
    progress: float = 0.0  # 0-1
    cost_so_far_usd: float = 0.0
    started_at: str | None = None
    estimated_eta_sec: int | None = None


class EventStreamItem(BaseModel):
    event_id: str
    event_type: str
    occurred_at: str
    summary: str
    severity: Literal["info", "warn", "error", "critical"] = "info"


class StateLedgerHistoryItem(BaseModel):
    """Durable history item replayed from EventRow."""

    event_id: str
    event_type: str
    occurred_at: str
    task_id: str | None = None
    summary: str = ""
    reason: str = ""
    cost_usd: float = 0.0
    decision_ticket_id: str | None = None
    decision_point: str = ""
    phase: str = ""
    selected_action: str = ""
    decision_status: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class StateLedgerStory(BaseModel):
    """Compact story replayed from durable State Ledger history."""

    task_id: str
    event_count: int = 0
    decision_count: int = 0
    world_action_count: int = 0
    external_action_count: int = 0
    total_cost_usd: float = 0.0
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    latest_event_type: str = ""
    latest_reason: str = ""
    status: str = "unknown"
    current_action: str = ""
    decision_summary: dict[str, int] = Field(default_factory=dict)
    decision_status_summary: dict[str, int] = Field(default_factory=dict)
    needs_review_decision_count: int = 0
    blocked_decision_count: int = 0
    pending_confirmations: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    decision_ticket_ids: list[str] = Field(default_factory=list)
    model_routes: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    context_asset_ids: list[str] = Field(default_factory=list)
    reconstruction_confidence: float = 0.0
    gaps: list[str] = Field(default_factory=list)
    timeline: list[StateLedgerHistoryItem] = Field(default_factory=list)


class StateLedgerAudit(BaseModel):
    """Compare current State Ledger snapshot with replayed durable events.

    这不是“完整事件溯源已完成”的证明，只是给 NUO/开发者一个诚实的
    漂移检查：当前快照和长期事件回放是否还能对上。
    """

    task_id: str
    tenant_id: str = ""
    snapshot_source: str = "missing"
    snapshot_found: bool = False
    replay_found: bool = False
    snapshot_status: str = "missing"
    replay_status: str = "unknown"
    status_matches: bool = False
    snapshot_updated_at: str | None = None
    replay_last_seen_at: str | None = None
    event_count: int = 0
    decision_count: int = 0
    snapshot_cost_usd: float = 0.0
    replay_cost_usd: float = 0.0
    cost_delta_usd: float = 0.0
    reconstruction_confidence: float = 0.0
    drift_detected: bool = False
    issues: list[str] = Field(default_factory=list)


class GlobalStateView(BaseModel):
    """全局状态区 (对人 5 个核心信息块)."""

    tenant_id: str
    user_id: str
    task_count_running: int = 0
    task_count_queued: int = 0
    total_cost_today_usd: float = 0.0
    total_cost_remaining_budget_usd: float = 0.0
    health_indicator: Literal["healthy", "warn", "critical"] = "healthy"
    urgent_alert_count: int = 0
    system_findings: list[dict[str, Any]] = Field(default_factory=list)
    active_state_ledger: list[StateLedgerEntry] = Field(default_factory=list)
    last_update: str = ""


class WorkspaceView(BaseModel):
    """共享工作区 (跨角色协作产物)."""

    task_id: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    handoff_packets: list[dict[str, Any]] = Field(default_factory=list)
    last_update: str


class AssetPoolSliceView(BaseModel):
    """资产池活跃切片 (当前任务涉及的)."""

    task_id: str
    pinned_assets: list[str] = Field(default_factory=list)
    semantic_assets: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)
    capability_card_refs: list[str] = Field(default_factory=list)


# ---- 数据源 hook (实际由 orchestrator / event store 注入) ----


_data_sources: dict[str, Callable[..., Any]] = {}


def register_data_source(key: str, fn: Callable[..., Any]) -> None:
    _data_sources[key] = fn


def reset_data_sources() -> None:
    _data_sources.clear()


# ---- 5 endpoint ----


async def _maybe_await(value: Any) -> Any:
    """Hooks 可以是 sync 或 async, 统一 await."""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


def _request_identity(
    *,
    x_user_id: str | None = None,
    x_tenant_id: str | None = None,
) -> tuple[str, str]:
    """Resolve blackboard identity from trusted tenant context.

    In production the middleware already resolves tenant/user from a signed
    Bearer token.  Blackboard endpoints must not silently trust X-Tenant-Id.
    In dev/test we keep the header fallback so old local scripts still work.
    """

    ctx = current_tenant()
    tenant_id = ctx.tenant_id
    if settings().env != "production" and x_tenant_id:
        tenant_id = x_tenant_id
    user_id = ctx.user_id or x_user_id or tenant_id
    return tenant_id, user_id


@router.get("/tasks", response_model=list[TaskBoardItem])
async def get_tasks(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    status: str | None = Query(None),
) -> list[TaskBoardItem]:
    """任务看板."""
    fn = _data_sources.get("tasks")
    if fn is None:
        return []
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    items = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id, status=status))
    return [TaskBoardItem(**i) if isinstance(i, dict) else i for i in items]


@router.get("/events", response_model=list[EventStreamItem])
async def get_events(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[EventStreamItem]:
    """事件流 (近期 N 条)."""
    fn = _data_sources.get("events")
    if fn is None:
        return []
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    items = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id, limit=limit))
    return [EventStreamItem(**i) if isinstance(i, dict) else i for i in items]


@router.get("/state", response_model=GlobalStateView)
async def get_state(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> GlobalStateView:
    """全局状态区."""
    fn = _data_sources.get("state")
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    if fn is None:
        return GlobalStateView(
            tenant_id=tenant_id,
            user_id=user_id,
            last_update=datetime.now(UTC).isoformat(),
        )
    state = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id))
    if isinstance(state, dict):
        return GlobalStateView(**state)
    if isinstance(state, GlobalStateView):
        return state
    raise TypeError(f"state data source returned unexpected type {type(state)}")


@router.get("/state-ledger", response_model=list[StateLedgerEntry])
async def get_state_ledger_list(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> list[StateLedgerEntry]:
    """当前活跃任务状态账本."""
    fn = _data_sources.get("state_ledger")
    if fn is None:
        return []
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    items = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id))
    return [StateLedgerEntry.model_validate(item) for item in items]


@router.get("/state-ledger/history", response_model=list[StateLedgerHistoryItem])
async def get_state_ledger_history(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[StateLedgerHistoryItem]:
    """长期状态账本：从 EventRow 回放最近事件。"""
    fn = _data_sources.get("state_ledger_history")
    if fn is None:
        return []
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    items = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id, limit=limit))
    return [StateLedgerHistoryItem.model_validate(item) for item in items]


@router.get("/state-ledger/{task_id}/history", response_model=list[StateLedgerHistoryItem])
async def get_state_ledger_task_history(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[StateLedgerHistoryItem]:
    """某个任务的长期状态账本：从 EventRow 回放。"""
    fn = _data_sources.get("state_ledger_history")
    if fn is None:
        return []
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    items = await _maybe_await(
        fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=limit)
    )
    return [StateLedgerHistoryItem.model_validate(item) for item in items]


@router.get("/state-ledger/{task_id}/story", response_model=StateLedgerStory)
async def get_state_ledger_task_story(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    limit: int = Query(100, ge=1, le=500),
) -> StateLedgerStory:
    """某个任务的长期状态故事：从 EventRow 回放成可读摘要。"""
    fn = _data_sources.get("state_ledger_story")
    if fn is None:
        history_fn = _data_sources.get("state_ledger_history")
        if history_fn is None:
            return StateLedgerStory(task_id=task_id)
        tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
        items = await _maybe_await(
            history_fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=limit)
        )
        history = [StateLedgerHistoryItem.model_validate(item) for item in items]
        return _story_from_history(task_id, history)
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    item = await _maybe_await(
        fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=limit)
    )
    return StateLedgerStory.model_validate(item)


@router.get("/state-ledger/{task_id}/audit", response_model=StateLedgerAudit)
async def get_state_ledger_task_audit(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    limit: int = Query(100, ge=1, le=500),
) -> StateLedgerAudit:
    """审计某个任务：当前快照和长期事件回放是否漂移."""
    fn = _data_sources.get("state_ledger_audit")
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    if fn is not None:
        item = await _maybe_await(
            fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=limit)
        )
        return StateLedgerAudit.model_validate(item)

    ledger_fn = _data_sources.get("state_ledger")
    story_fn = _data_sources.get("state_ledger_story")
    snapshot = (
        await _maybe_await(ledger_fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id))
        if ledger_fn
        else None
    )
    story = (
        await _maybe_await(
            story_fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=limit)
        )
        if story_fn
        else {"task_id": task_id}
    )
    return _audit_from_snapshot_and_story(
        task_id=task_id,
        tenant_id=tenant_id,
        snapshot=snapshot,
        story=story,
        snapshot_source="blackboard",
    )


@router.get("/state-ledger/{task_id}", response_model=StateLedgerEntry)
async def get_state_ledger_task(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> StateLedgerEntry:
    """某个任务的状态账本快照."""
    fn = _data_sources.get("state_ledger")
    if fn is None:
        raise HTTPException(404, "state ledger not found")
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    item = await _maybe_await(fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id))
    if item is None:
        raise HTTPException(404, "state ledger not found")
    return StateLedgerEntry.model_validate(item)


@router.get("/workspace/{task_id}", response_model=WorkspaceView)
async def get_workspace(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> WorkspaceView:
    """共享工作区."""
    fn = _data_sources.get("workspace")
    if fn is None:
        return WorkspaceView(
            task_id=task_id,
            last_update=datetime.now(UTC).isoformat(),
        )
    _tenant_id, user_id = _request_identity(x_user_id=x_user_id)
    ws = await _maybe_await(fn(task_id=task_id, user_id=user_id))
    if ws is None:
        raise HTTPException(404, "workspace not found")
    if isinstance(ws, dict):
        return WorkspaceView(**ws)
    if isinstance(ws, WorkspaceView):
        return ws
    raise TypeError(f"workspace data source returned unexpected type {type(ws)}")


@router.get("/assets/{task_id}", response_model=AssetPoolSliceView)
async def get_assets(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> AssetPoolSliceView:
    """资产池活跃切片."""
    fn = _data_sources.get("assets")
    if fn is None:
        return AssetPoolSliceView(task_id=task_id)
    _tenant_id, user_id = _request_identity(x_user_id=x_user_id)
    assets = await _maybe_await(fn(task_id=task_id, user_id=user_id))
    if isinstance(assets, dict):
        return AssetPoolSliceView(**assets)
    if isinstance(assets, AssetPoolSliceView):
        return assets
    raise TypeError(f"assets data source returned unexpected type {type(assets)}")


# 对 agent 的全量 JSON dump (双重渲染另一面)
@router.get("/full/{task_id}")
async def get_full_for_agent(
    task_id: str,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> dict[str, Any]:
    """对 agent: 完整 JSON dump (state + workspace + assets + events)."""
    state_fn = _data_sources.get("state")
    ledger_fn = _data_sources.get("state_ledger")
    ws_fn = _data_sources.get("workspace")
    assets_fn = _data_sources.get("assets")
    events_fn = _data_sources.get("events")
    tenant_id, user_id = _request_identity(x_user_id=x_user_id, x_tenant_id=x_tenant_id)
    return {
        "rendered_for": "agent",
        "task_id": task_id,
        "state": (
            await _maybe_await(state_fn(tenant_id=tenant_id, user_id=user_id)) if state_fn else {}
        ),
        "state_ledger": (
            await _maybe_await(ledger_fn(tenant_id=tenant_id, user_id=user_id, task_id=task_id))
            if ledger_fn
            else {}
        ),
        "state_ledger_history": (
            await _maybe_await(
                _data_sources["state_ledger_history"](
                    tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=100
                )
            )
            if "state_ledger_history" in _data_sources
            else []
        ),
        "state_ledger_story": (
            await _maybe_await(
                _data_sources["state_ledger_story"](
                    tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=100
                )
            )
            if "state_ledger_story" in _data_sources
            else {}
        ),
        "state_ledger_audit": (
            await _maybe_await(
                _data_sources["state_ledger_audit"](
                    tenant_id=tenant_id, user_id=user_id, task_id=task_id, limit=100
                )
            )
            if "state_ledger_audit" in _data_sources
            else {}
        ),
        "workspace": (await _maybe_await(ws_fn(task_id=task_id, user_id=user_id)) if ws_fn else {}),
        "assets": (
            await _maybe_await(assets_fn(task_id=task_id, user_id=user_id)) if assets_fn else {}
        ),
        "events": (
            await _maybe_await(events_fn(tenant_id=tenant_id, user_id=user_id, limit=100))
            if events_fn
            else []
        ),
        "rendered_at": datetime.now(UTC).isoformat(),
    }


def _story_from_history(
    task_id: str,
    history: list[StateLedgerHistoryItem],
) -> StateLedgerStory:
    return StateLedgerStory.model_validate(
        replay_state_ledger_story(
            task_id,
            [item.model_dump(mode="json") for item in history],
            timeline_limit=20,
        )
    )


def _audit_from_snapshot_and_story(
    *,
    task_id: str,
    tenant_id: str,
    snapshot: Any,
    story: Any,
    snapshot_source: str,
) -> StateLedgerAudit:
    snapshot_data = _model_or_dict(snapshot)
    story_data = _model_or_dict(story)
    snapshot_found = bool(snapshot_data)
    replay_found = int(story_data.get("event_count") or 0) > 0
    snapshot_status = str(snapshot_data.get("status") or "missing")
    replay_status = str(story_data.get("status") or "unknown")
    status_matches = snapshot_found and replay_found and snapshot_status == replay_status
    snapshot_cost = _float_value(snapshot_data.get("cost_so_far_usd"))
    replay_cost = _float_value(story_data.get("total_cost_usd"))
    cost_delta = round(snapshot_cost - replay_cost, 6)
    issues: list[str] = []
    if not snapshot_found:
        issues.append("missing_current_snapshot")
    if not replay_found:
        issues.append("missing_durable_history")
    if snapshot_found and replay_found and not status_matches:
        issues.append("status_drift")
    if abs(cost_delta) > 0.01:
        issues.append("cost_drift")
    for gap in story_data.get("gaps", []) if isinstance(story_data.get("gaps"), list) else []:
        if gap:
            issues.append(str(gap))
    return StateLedgerAudit(
        task_id=task_id,
        tenant_id=tenant_id,
        snapshot_source=snapshot_source if snapshot_found else "missing",
        snapshot_found=snapshot_found,
        replay_found=replay_found,
        snapshot_status=snapshot_status,
        replay_status=replay_status,
        status_matches=status_matches,
        snapshot_updated_at=_optional_text(snapshot_data.get("updated_at")),
        replay_last_seen_at=_optional_text(story_data.get("last_seen_at")),
        event_count=int(story_data.get("event_count") or 0),
        decision_count=int(story_data.get("decision_count") or 0),
        snapshot_cost_usd=snapshot_cost,
        replay_cost_usd=replay_cost,
        cost_delta_usd=cost_delta,
        reconstruction_confidence=_float_value(story_data.get("reconstruction_confidence")),
        drift_detected=any(item.endswith("_drift") for item in issues),
        issues=issues,
    )


def _model_or_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {}


def _float_value(value: Any) -> float:
    try:
        return round(float(value or 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "AssetPoolSliceView",
    "EventStreamItem",
    "GlobalStateView",
    "StateLedgerAudit",
    "StateLedgerEntry",
    "StateLedgerHistoryItem",
    "StateLedgerStory",
    "TaskBoardItem",
    "WorkspaceView",
    "register_data_source",
    "reset_data_sources",
    "router",
]
