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


@router.get("/tasks", response_model=list[TaskBoardItem])
def get_tasks(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
    status: str | None = Query(None),
) -> list[TaskBoardItem]:
    """任务看板."""
    fn = _data_sources.get("tasks")
    if fn is None:
        return []
    items = fn(tenant_id=x_tenant_id, user_id=x_user_id, status=status)
    return [TaskBoardItem(**i) if isinstance(i, dict) else i for i in items]


@router.get("/events", response_model=list[EventStreamItem])
def get_events(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
    limit: int = Query(50, ge=1, le=500),
) -> list[EventStreamItem]:
    """事件流 (近期 N 条)."""
    fn = _data_sources.get("events")
    if fn is None:
        return []
    items = fn(tenant_id=x_tenant_id, user_id=x_user_id, limit=limit)
    return [EventStreamItem(**i) if isinstance(i, dict) else i for i in items]


@router.get("/state", response_model=GlobalStateView)
def get_state(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> GlobalStateView:
    """全局状态区."""
    fn = _data_sources.get("state")
    if fn is None:
        return GlobalStateView(
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            last_update=datetime.now(UTC).isoformat(),
        )
    state = fn(tenant_id=x_tenant_id, user_id=x_user_id)
    if isinstance(state, dict):
        return GlobalStateView(**state)
    if isinstance(state, GlobalStateView):
        return state
    raise TypeError(f"state data source returned unexpected type {type(state)}")


@router.get("/workspace/{task_id}", response_model=WorkspaceView)
def get_workspace(
    task_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> WorkspaceView:
    """共享工作区."""
    fn = _data_sources.get("workspace")
    if fn is None:
        return WorkspaceView(
            task_id=task_id,
            last_update=datetime.now(UTC).isoformat(),
        )
    ws = fn(task_id=task_id, user_id=x_user_id)
    if ws is None:
        raise HTTPException(404, "workspace not found")
    if isinstance(ws, dict):
        return WorkspaceView(**ws)
    if isinstance(ws, WorkspaceView):
        return ws
    raise TypeError(f"workspace data source returned unexpected type {type(ws)}")


@router.get("/assets/{task_id}", response_model=AssetPoolSliceView)
def get_assets(
    task_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> AssetPoolSliceView:
    """资产池活跃切片."""
    fn = _data_sources.get("assets")
    if fn is None:
        return AssetPoolSliceView(task_id=task_id)
    assets = fn(task_id=task_id, user_id=x_user_id)
    if isinstance(assets, dict):
        return AssetPoolSliceView(**assets)
    if isinstance(assets, AssetPoolSliceView):
        return assets
    raise TypeError(f"assets data source returned unexpected type {type(assets)}")


# 对 agent 的全量 JSON dump (双重渲染另一面)
@router.get("/full/{task_id}")
def get_full_for_agent(
    task_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> dict[str, Any]:
    """对 agent: 完整 JSON dump (state + workspace + assets + events)."""
    state_fn = _data_sources.get("state")
    ws_fn = _data_sources.get("workspace")
    assets_fn = _data_sources.get("assets")
    events_fn = _data_sources.get("events")
    return {
        "rendered_for": "agent",
        "task_id": task_id,
        "state": state_fn(tenant_id="-", user_id=x_user_id) if state_fn else {},
        "workspace": ws_fn(task_id=task_id, user_id=x_user_id) if ws_fn else {},
        "assets": assets_fn(task_id=task_id, user_id=x_user_id) if assets_fn else {},
        "events": events_fn(tenant_id="-", user_id=x_user_id, limit=100) if events_fn else [],
        "rendered_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "AssetPoolSliceView",
    "EventStreamItem",
    "GlobalStateView",
    "TaskBoardItem",
    "WorkspaceView",
    "register_data_source",
    "reset_data_sources",
    "router",
]
