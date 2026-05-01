from __future__ import annotations

import pytest
from kun.context.assets import LayeredAsset
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.memory_invocation_policy import (
    MemoryInvocationInput,
    decide_memory_invocation,
    decide_memory_invocation_for_task,
)
from kun.memory.policy import MemoryDepth, MemoryLayer
from kun.watchtower.decision_plane import WatchtowerDecisionPlane


def _task(
    *,
    task_type: str,
    text: str,
    risk: str = "low",
    complexity: float = 0.2,
) -> TaskRef:
    owner = Owner(tenant_id="tenant-memory-invoke", user_id="u-memory-invoke")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint(text, owner),
            task_id=f"task-{task_type.replace('.', '-')}",
            task_type=task_type,
            risk_level=risk,  # type: ignore[arg-type]
            complexity_score=complexity,
            success_criteria_short=text,
            owner=owner,
        ),
        spec=TaskSpec(goal_detail=text, success_metrics=["完成目标"]),
    )


@pytest.mark.unit
def test_memory_invocation_keeps_simple_query_on_fast_lane() -> None:
    ticket = decide_memory_invocation_for_task(
        _task(task_type="general.query", text="查询一下当前状态", complexity=0.08)
    )

    assert ticket.use_memory is False
    assert ticket.memory_depth == MemoryDepth.NO_MEMORY
    assert MemoryLayer.EXECUTION_PROCESS in ticket.avoid_memory_layers
    assert "simple_task_fast_lane=no_memory" in ticket.reasons


@pytest.mark.unit
def test_memory_invocation_coding_task_sparse_activates_process_and_skill_context() -> None:
    ticket = decide_memory_invocation_for_task(
        _task(
            task_type="coding.bugfix",
            text="修复 pytest failure in markdown parser",
            complexity=0.52,
        )
    )

    assert ticket.use_memory is True
    assert ticket.memory_depth == MemoryDepth.TARGETED
    assert ticket.memory_layers[:2] == [
        MemoryLayer.EXECUTION_PROCESS,
        MemoryLayer.META_DECISION,
    ]
    assert "skill" in ticket.asset_kinds
    assert "pytest" in ticket.strategy_tags


@pytest.mark.unit
def test_memory_invocation_retry_deepens_memory_without_waking_everything() -> None:
    ticket = decide_memory_invocation_for_task(
        _task(
            task_type="writing.ad",
            text="上一次广告文案失败了，重新来",
            complexity=0.35,
        ),
        previous_failure=True,
    )

    assert ticket.memory_depth == MemoryDepth.DEEP
    assert ticket.max_items >= 5
    assert MemoryLayer.EXECUTION_PROCESS in ticket.memory_layers
    assert MemoryLayer.TASK_RESULT in ticket.memory_layers
    assert "retry" in ticket.strategy_tags


@pytest.mark.unit
def test_memory_invocation_marketing_task_uses_creative_and_conversion_memory() -> None:
    ticket = decide_memory_invocation_for_task(
        _task(
            task_type="content.ad_video",
            text="写一条短视频广告文案，重点优化 hook、CTR 和转化",
            complexity=0.42,
        )
    )

    assert ticket.use_memory is True
    assert ticket.memory_depth == MemoryDepth.TARGETED
    assert ticket.memory_layers[:2] == [
        MemoryLayer.METHODOLOGY,
        MemoryLayer.BEHAVIOR,
    ]
    assert "skill" in ticket.asset_kinds
    assert "hook" in ticket.strategy_tags
    assert "conversion" in ticket.strategy_tags
    assert "ctr" in ticket.strategy_tags


@pytest.mark.unit
def test_memory_invocation_high_risk_external_task_avoids_behavior_memory() -> None:
    ticket = decide_memory_invocation_for_task(
        _task(
            task_type="world.email",
            text="给客户发送报价邮件",
            risk="high",
            complexity=0.45,
        )
    )

    assert ticket.high_risk_task is True
    assert ticket.memory_depth in {MemoryDepth.TARGETED, MemoryDepth.DEEP}
    assert MemoryLayer.BEHAVIOR in ticket.avoid_memory_layers
    assert "approval" in ticket.strategy_tags


@pytest.mark.unit
def test_memory_invocation_uses_historical_credit_as_sparse_hint() -> None:
    ticket = decide_memory_invocation(
        MemoryInvocationInput(
            task_type="general",
            text="给一个产品增长方案",
            complexity_score=0.4,
            historical_resource_credit={
                "asset_kind:skill": 0.8,
                "memory_layer:meta_decision": 0.9,
                "memory_layer:behavior": -0.5,
                "tag:growth": 0.7,
            },
        )
    )

    assert "skill" in ticket.asset_kinds
    assert ticket.memory_layers[0] == MemoryLayer.META_DECISION
    assert MemoryLayer.BEHAVIOR in ticket.avoid_memory_layers
    assert "growth" in ticket.strategy_tags


@pytest.mark.unit
def test_watchtower_decision_plane_exposes_memory_invocation_ticket() -> None:
    decision = WatchtowerDecisionPlane().decide(
        _task(
            task_type="coding.bugfix",
            text="修复 ruff 和 pytest 报错",
            complexity=0.6,
        )
    )

    invocation = decision.metadata["memory_invocation_policy"]
    policy = decision.metadata["memory_policy"]
    assert invocation["use_memory"] is True
    assert invocation["memory_depth"] in {"targeted", "deep"}
    assert "execution_process" in invocation["memory_layers"]
    assert policy["layers"] == invocation["memory_layers"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_invocation_kwargs_filter_context_packer_layers() -> None:
    store = InMemoryAssetStore()
    for title, layer in [
        ("process", "execution_process"),
        ("meta", "meta_decision"),
        ("result", "task_result"),
    ]:
        await store.put(
            LayeredAsset.build(
                asset_kind="memory",
                tenant_id="tenant-memory-invoke",
                metadata={"title": title, "memory_layer": layer},
                summary=f"pytest payment parser {title} memory",
                tags=["pytest", layer],
            )
        )

    task = _task(
        task_type="coding.bugfix",
        text="修复 pytest payment parser",
        complexity=0.52,
    )
    ticket = decide_memory_invocation_for_task(task)
    kwargs = ticket.as_context_packer_kwargs()

    pack = await ContextPacker(store).pack(
        task,
        tenant_id="tenant-memory-invoke",
        kinds=kwargs["kinds"],
        limit=kwargs["limit"],
        memory_layers=kwargs["memory_layers"],
        avoid_memory_layers=kwargs["avoid_memory_layers"],
        preferred_tags=kwargs["preferred_tags"],
        high_risk_task=kwargs["high_risk_task"],
    )

    assert {item.memory_layer for item in pack.items} <= {
        "execution_process",
        "meta_decision",
        "behavior",
        "methodology",
    }
    assert "task_result" not in {item.memory_layer for item in pack.items}
