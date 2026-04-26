"""C24-c anchor-expand adapters for idle-batch and attention anchors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.core.attention_anchor import AttentionAnchor, AttentionManager
from kun.engineering.idle_batch import (
    IdleBatchStep,
    register_step,
    run_all_anchor_then_expand,
)


async def _collect(async_iter) -> list:
    items = []
    async for item in async_iter:
        items.append(item)
    return items


class _Step(IdleBatchStep):
    def __init__(self, step_id: str, *, fail: bool = False) -> None:
        self.step_id = step_id
        self.fail = fail
        self.calls = 0

    async def run(self, tenant_id: str) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return {"tenant_id": tenant_id, "calls": self.calls}


@pytest.mark.asyncio
async def test_idle_batch_anchor_runs_one_step_when_max_rounds_one() -> None:
    step = _Step("c24c_anchor_one")
    register_step(step)

    reports = await _collect(
        run_all_anchor_then_expand(
            "t1",
            enabled={"c24c_anchor_one"},
            max_rounds=1,
        )
    )

    assert [r.step_id for r in reports] == ["c24c_anchor_one"]
    assert reports[0].status == "ok"
    assert step.calls == 1


@pytest.mark.asyncio
async def test_idle_batch_anchor_expands_in_priority_order() -> None:
    # 这两个是默认注册 step, priority 里 health_report 排在 task_replay 前.
    reports = await _collect(
        run_all_anchor_then_expand(
            "t1",
            enabled={"task_replay", "health_report"},
            max_rounds=2,
        )
    )

    assert [r.step_id for r in reports] == ["health_report", "task_replay"]


@pytest.mark.asyncio
async def test_idle_batch_anchor_keeps_failure_as_report() -> None:
    step = _Step("c24c_anchor_fail", fail=True)
    register_step(step)

    reports = await _collect(
        run_all_anchor_then_expand(
            "t1",
            enabled={"c24c_anchor_fail"},
            max_rounds=1,
        )
    )

    assert reports[0].status == "failed"
    assert "boom" in reports[0].summary["error"]


@pytest.mark.asyncio
async def test_idle_batch_anchor_empty_enabled_yields_nothing() -> None:
    reports = await _collect(
        run_all_anchor_then_expand(
            "t1",
            enabled={"not_registered"},
            max_rounds=3,
        )
    )

    assert reports == []


@pytest.mark.asyncio
async def test_attention_anchor_yields_permanent_redline_first() -> None:
    manager = AttentionManager()
    manager.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="asset-a"))
    manager.add(
        AttentionAnchor(
            anchor_kind="permanent_redline",
            target_asset_ref="asset-b",
            weight_boost=0.2,
        )
    )

    anchors = await _collect(
        manager.must_check_for_decision_anchor_then_expand("model_select", max_rounds=1)
    )

    assert [a.anchor_kind for a in anchors] == ["permanent_redline"]


@pytest.mark.asyncio
async def test_attention_anchor_expands_without_duplicates() -> None:
    manager = AttentionManager()
    manager.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="asset-a"))
    manager.add(AttentionAnchor(anchor_kind="task_dependency", target_asset_ref="asset-b"))
    manager.add(AttentionAnchor(anchor_kind="permanent_redline", target_asset_ref="asset-c"))

    anchors = await _collect(
        manager.must_check_for_decision_anchor_then_expand("model_select", max_rounds=3)
    )

    assert [a.anchor_kind for a in anchors] == [
        "permanent_redline",
        "task_dependency",
        "user_pin",
    ]
    assert len({a.anchor_id for a in anchors}) == 3


@pytest.mark.asyncio
async def test_attention_anchor_ignores_expired_anchor() -> None:
    manager = AttentionManager()
    manager.add(
        AttentionAnchor(
            anchor_kind="permanent_redline",
            target_asset_ref="expired",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    manager.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="live"))

    anchors = await _collect(
        manager.must_check_for_decision_anchor_then_expand("model_select", max_rounds=3)
    )

    assert [a.target_asset_ref for a in anchors] == ["live"]


@pytest.mark.asyncio
async def test_attention_anchor_unknown_decision_only_redline() -> None:
    manager = AttentionManager()
    manager.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="asset-a"))
    manager.add(AttentionAnchor(anchor_kind="permanent_redline", target_asset_ref="asset-b"))

    anchors = await _collect(
        manager.must_check_for_decision_anchor_then_expand("unknown_decision", max_rounds=3)
    )

    assert [a.anchor_kind for a in anchors] == ["permanent_redline"]
