"""AttentionAnchor — 全局视角注意力实现 (V2 §18 / ADR-020 / §16.11).

合并:
- 用户 pin (§3.5 tier 1)
- 项目 context anchor (§13.7)
- 永久档红线 (§3.5 tier 0)
- 任务依赖 (§3.2)
- session 启动加载 (§18.5)

5 类锚定走同一抽象, 重要度打分 / 跨会话恢复 / 元认知自检统一一处.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.ids import new_id

AnchorKind = Literal[
    "user_pin",  # §3.5 tier 1 用户显式 pin
    "project_context",  # 项目级 anchor
    "permanent_redline",  # §3.5 tier 0 永久档红线
    "task_dependency",  # §3.2 任务硬依赖
    "session_bootstrap",  # §18.5 session 启动加载
]

AnchorScope = Literal["user", "project", "tenant", "global"]

CreatedBy = Literal["user_explicit", "system_inferred", "policy_required"]


class AttentionAnchor(BaseModel):
    """注意力锚定."""

    anchor_id: str = Field(default_factory=lambda: new_id("aa"))
    anchor_kind: AnchorKind
    target_asset_ref: str  # 指向 LayeredAsset
    weight_boost: float = Field(ge=0.0, le=0.5, default=0.15)
    scope: AnchorScope = "user"
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: CreatedBy = "user_explicit"
    reason: str = ""
    tenant_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None


class AttentionManager:
    """注意力锚定管理 (内存 + 可持久化扩展点)."""

    def __init__(self) -> None:
        self._anchors: dict[str, AttentionAnchor] = {}

    def add(self, anchor: AttentionAnchor) -> None:
        self._anchors[anchor.anchor_id] = anchor

    def remove(self, anchor_id: str) -> bool:
        return self._anchors.pop(anchor_id, None) is not None

    def get(self, anchor_id: str) -> AttentionAnchor | None:
        return self._anchors.get(anchor_id)

    def list_for_user(
        self,
        user_id: str,
        kinds: tuple[AnchorKind, ...] | None = None,
    ) -> list[AttentionAnchor]:
        """取该 user 的所有锚定 (按 kind 过滤可选)."""
        now = datetime.now(UTC)
        out = []
        for a in self._anchors.values():
            if a.user_id and a.user_id != user_id:
                continue
            if a.expires_at and a.expires_at < now:
                continue
            if kinds and a.anchor_kind not in kinds:
                continue
            out.append(a)
        return out

    def list_for_project(
        self,
        project_id: str,
        kinds: tuple[AnchorKind, ...] | None = None,
    ) -> list[AttentionAnchor]:
        """取该 project 的所有锚定."""
        now = datetime.now(UTC)
        out = []
        for a in self._anchors.values():
            if a.project_id and a.project_id != project_id:
                continue
            if a.expires_at and a.expires_at < now:
                continue
            if kinds and a.anchor_kind not in kinds:
                continue
            out.append(a)
        return out

    def boost_for_asset(
        self,
        asset_ref: str,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> float:
        """计算该 asset 在打分时的加权."""
        boost = 0.0
        for a in self._anchors.values():
            if a.target_asset_ref != asset_ref:
                continue
            if a.user_id is not None and a.user_id != user_id:
                continue
            if a.project_id is not None and a.project_id != project_id:
                continue
            if a.expires_at and a.expires_at < datetime.now(UTC):
                continue
            boost = max(boost, a.weight_boost)  # 取最高加权 (避免重复加和爆表)
        return boost

    def must_check_for_decision(
        self,
        decision_kind: str,
    ) -> list[AttentionAnchor]:
        """决策前必查的锚定 (按 §18.3 全局扫描清单)."""
        kinds: tuple[AnchorKind, ...]
        if decision_kind in ("model_select", "evaluation_tier", "ask_user_trigger"):
            kinds = ("user_pin", "permanent_redline", "task_dependency")
        elif decision_kind in ("plan_only_trigger", "escalation_level"):
            kinds = ("user_pin", "permanent_redline")
        else:
            kinds = ("permanent_redline",)
        now = datetime.now(UTC)
        return [
            a
            for a in self._anchors.values()
            if a.anchor_kind in kinds and (a.expires_at is None or a.expires_at >= now)
        ]

    async def must_check_for_decision_anchor_then_expand(
        self,
        decision_kind: str,
        *,
        max_rounds: int = 3,
    ) -> AsyncIterator[AttentionAnchor]:
        """按需返回决策前必查锚点.

        老的 ``must_check_for_decision`` 一次性返回所有锚点. 新接口先返回最高优先级
        锚点, 下游需要更多时再 expand.

        # TODO: wire by Claude in V2.2
        """
        anchors = sorted(
            self.must_check_for_decision(decision_kind),
            key=_anchor_priority,
            reverse=True,
        )
        if not anchors:
            return

        async def anchor_fn() -> AttentionAnchor:
            return anchors[0]

        async def expand_fn(
            _anchor: AttentionAnchor,
            prior: list[AttentionAnchor],
        ) -> AttentionAnchor | None:
            seen = {item.anchor_id for item in prior}
            return next((item for item in anchors if item.anchor_id not in seen), None)

        async for anchor in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield anchor


def _anchor_priority(anchor: AttentionAnchor) -> tuple[int, float]:
    kind_order = {
        "permanent_redline": 5,
        "task_dependency": 4,
        "project_context": 3,
        "session_bootstrap": 2,
        "user_pin": 1,
    }
    return (kind_order.get(anchor.anchor_kind, 0), anchor.weight_boost)


_manager: AttentionManager | None = None


def get_manager() -> AttentionManager:
    global _manager
    if _manager is None:
        _manager = AttentionManager()
    return _manager


def reset_manager() -> None:
    global _manager
    _manager = None


__all__ = [
    "AnchorKind",
    "AnchorScope",
    "AttentionAnchor",
    "AttentionManager",
    "CreatedBy",
    "get_manager",
    "reset_manager",
]
