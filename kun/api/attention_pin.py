"""User pin API — 用户显式锚定 (V2.1 §3.5 tier 1 + §18.4 + T17 子项).

POST /api/preferences/pin       创建 pin
DELETE /api/preferences/pin/{id} 解除 pin
GET /api/preferences/pin        列出 user 的所有 pin
GET /api/preferences/pin/anchors 列出 AttentionAnchor (含 boost)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from kun.context.importance import PIN_HALF_LIFE_DAYS
from kun.core.attention_anchor import (
    AttentionAnchor,
    get_manager,
)


class PinCreateRequest(BaseModel):
    """请求体: 创建 pin."""

    target_asset_ref: str
    weight_boost: float = Field(ge=0.0, le=0.5, default=0.15)
    reason: str = ""
    scope: Literal["user", "project", "tenant"] = "user"
    project_id: str | None = None
    expires_in_days: int = int(PIN_HALF_LIFE_DAYS)  # 默认 90 天


class PinResponse(BaseModel):
    """响应: 创建 / 取 pin."""

    anchor_id: str
    anchor_kind: Literal["user_pin"] = "user_pin"
    target_asset_ref: str
    weight_boost: float
    user_id: str
    expires_at: str | None
    created_at: str
    reason: str


class PinListResponse(BaseModel):
    """响应: 列表."""

    user_id: str
    pin_count: int
    pins: list[PinResponse]


router = APIRouter(prefix="/api/preferences", tags=["attention-pin"])


@router.post("/pin", response_model=PinResponse, status_code=status.HTTP_201_CREATED)
def create_pin(
    body: PinCreateRequest,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> PinResponse:
    """用户显式 pin 一个资产.

    pin 后:
    - 资产打分获得 weight_boost 加权 (§3.2)
    - 跨会话强制加载 (§18.5 SessionInit)
    - 90 天半衰期 (默认)
    """
    expires = datetime.now(UTC) + timedelta(days=body.expires_in_days)
    anchor = AttentionAnchor(
        anchor_kind="user_pin",
        target_asset_ref=body.target_asset_ref,
        weight_boost=body.weight_boost,
        scope=body.scope,
        expires_at=expires,
        created_by="user_explicit",
        reason=body.reason,
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        project_id=body.project_id,
    )
    get_manager().add(anchor)
    return _to_response(anchor)


@router.delete("/pin/{anchor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pin(
    anchor_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> None:
    """解除 pin."""
    mgr = get_manager()
    anchor = mgr.get(anchor_id)
    if anchor is None:
        raise HTTPException(status_code=404, detail="anchor not found")
    if anchor.user_id and anchor.user_id != x_user_id:
        raise HTTPException(status_code=403, detail="not your anchor")
    mgr.remove(anchor_id)


@router.get("/pin", response_model=PinListResponse)
def list_pins(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> PinListResponse:
    """列出 user 的所有 pin."""
    anchors = get_manager().list_for_user(x_user_id, kinds=("user_pin",))
    return PinListResponse(
        user_id=x_user_id,
        pin_count=len(anchors),
        pins=[_to_response(a) for a in anchors],
    )


@router.get("/pin/anchors")
def list_anchors_with_boost(
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    asset_ref: str | None = None,
) -> dict[str, object]:
    """列出该 user 所有有效 anchor 及其 boost 值. (调试用)"""
    mgr = get_manager()
    anchors = mgr.list_for_user(x_user_id)
    out = {
        "user_id": x_user_id,
        "anchor_count": len(anchors),
        "anchors": [
            {
                "anchor_id": a.anchor_id,
                "kind": a.anchor_kind,
                "target": a.target_asset_ref,
                "weight_boost": a.weight_boost,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a in anchors
        ],
    }
    if asset_ref:
        out["boost_for_asset"] = mgr.boost_for_asset(asset_ref, user_id=x_user_id)
    return out


def _to_response(a: AttentionAnchor) -> PinResponse:
    return PinResponse(
        anchor_id=a.anchor_id,
        anchor_kind="user_pin",
        target_asset_ref=a.target_asset_ref,
        weight_boost=a.weight_boost,
        user_id=a.user_id or "",
        expires_at=a.expires_at.isoformat() if a.expires_at else None,
        created_at=a.created_at.isoformat(),
        reason=a.reason,
    )


__all__ = [
    "PinCreateRequest",
    "PinListResponse",
    "PinResponse",
    "router",
]
