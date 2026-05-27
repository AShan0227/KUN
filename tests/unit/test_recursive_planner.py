"""LT.F — RecursivePlanner 单测."""

from __future__ import annotations

import pytest
from kun.agents.director.recursive_planner import (
    PlanNode,
    PlanStepInput,
    PlanTree,
    RecursivePlanner,
)

# ---- Construction validation ----


def test_constructor_validates_max_depth() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        RecursivePlanner(max_depth=0)


def test_constructor_validates_max_breadth() -> None:
    with pytest.raises(ValueError, match="max_breadth"):
        RecursivePlanner(max_breadth_per_node=0)


# ---- Tree build basics ----


@pytest.mark.asyncio
async def test_expand_with_atomic_top_steps() -> None:
    """有 success_criterion 的 step → atomic, 不再递归."""
    planner = RecursivePlanner()
    steps = [
        PlanStepInput(
            description="step 1", success_criterion="step 1 done"
        ),
        PlanStepInput(
            description="step 2", success_criterion="step 2 done"
        ),
    ]
    tree = await planner.expand(
        root_description="my task",
        root_steps=steps,
    )
    assert isinstance(tree, PlanTree)
    assert tree.root().description == "my task"
    assert tree.root().depth == 0
    assert len(tree.children_of(tree.root_id)) == 2
    # 所有 children 都是 atomic (因为 success_criterion 已指定)
    leaves = tree.leaves()
    assert len(leaves) == 2
    assert all(leaf.is_atomic for leaf in leaves)


@pytest.mark.asyncio
async def test_expand_recurses_when_not_atomic() -> None:
    """没 success_criterion 且描述长 → 递归 sub_planner."""
    planner = RecursivePlanner(max_depth=2)
    steps = [
        PlanStepInput(
            description="implement OAuth login with PKCE and session refresh"
        ),
    ]
    tree = await planner.expand(
        root_description="big task",
        root_steps=steps,
    )
    # root + 1 mid + 3 grandchildren (default sub_planner 拆 3 步) = 5 nodes
    assert tree.node_count() >= 5
    # children of root
    mid_nodes = tree.children_of(tree.root_id)
    assert len(mid_nodes) == 1
    # mid node not atomic, has children
    mid = mid_nodes[0]
    assert mid.is_atomic is False
    assert len(mid.children_ids) == 3
    # grandchildren depth 2 → atomic (max_depth)
    grands = tree.children_of(mid.node_id)
    for g in grands:
        assert g.depth == 2
        assert g.is_atomic is True


@pytest.mark.asyncio
async def test_max_depth_forces_atomic() -> None:
    """max_depth=1 → 第一层 children 即使 sub_planner 也不再递归."""
    planner = RecursivePlanner(max_depth=1)
    tree = await planner.expand(
        root_description="task",
        root_steps=[PlanStepInput(description="long step that would normally recurse")],
    )
    # max_depth=1 → 第一层 children 全 atomic
    children = tree.children_of(tree.root_id)
    assert all(c.is_atomic for c in children)
    assert tree.depth() == 1


@pytest.mark.asyncio
async def test_max_breadth_truncates() -> None:
    """root_steps 超过 max_breadth → 截断."""
    planner = RecursivePlanner(max_breadth_per_node=3)
    steps = [
        PlanStepInput(description=f"step {i}", success_criterion=f"sc {i}")
        for i in range(10)
    ]
    tree = await planner.expand(root_description="task", root_steps=steps)
    children = tree.children_of(tree.root_id)
    assert len(children) == 3


@pytest.mark.asyncio
async def test_atomic_decider_skill_hint_tool_prefix_is_atomic() -> None:
    """skill_hint='tool.x' → atomic (直接调具体 tool)."""
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="task",
        root_steps=[
            PlanStepInput(
                description="long description that would normally recurse if no skill",
                skill_hint="tool.bash",
            )
        ],
    )
    children = tree.children_of(tree.root_id)
    assert len(children) == 1
    assert children[0].is_atomic is True


@pytest.mark.asyncio
async def test_atomic_decider_short_description_is_atomic() -> None:
    """description ≤ 12 字符 → atomic."""
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="task",
        root_steps=[PlanStepInput(description="run tests")],  # 9 chars
    )
    children = tree.children_of(tree.root_id)
    assert children[0].is_atomic is True


# ---- Custom callbacks ----


@pytest.mark.asyncio
async def test_custom_atomic_decider() -> None:
    """Caller 注入 atomic_decider — 不用默认启发."""

    def always_atomic(_node: PlanNode) -> bool:
        return True

    planner = RecursivePlanner(atomic_decider=always_atomic)
    tree = await planner.expand(
        root_description="task",
        root_steps=[PlanStepInput(description="some longish description here")],
    )
    # all children atomic since decider returns True
    children = tree.children_of(tree.root_id)
    assert all(c.is_atomic for c in children)


@pytest.mark.asyncio
async def test_custom_sub_planner_used_when_not_atomic() -> None:
    """Caller 注入 sub_planner — atomic_decider=False 时被调."""
    calls: list[PlanNode] = []

    async def my_sub_planner(node: PlanNode) -> list[PlanStepInput]:
        calls.append(node)
        return [
            PlanStepInput(description="custom child A", success_criterion="A done"),
            PlanStepInput(description="custom child B", success_criterion="B done"),
        ]

    def atomic_at_depth_2(node: PlanNode) -> bool:
        # depth 1 still recurses (not atomic); depth 2 is atomic
        return node.depth >= 2

    planner = RecursivePlanner(
        max_depth=3, atomic_decider=atomic_at_depth_2, sub_planner=my_sub_planner
    )
    tree = await planner.expand(
        root_description="task",
        root_steps=[PlanStepInput(description="step needing sub-planning")],
    )
    assert len(calls) == 1  # only the top step expands
    # Custom sub-planner returned 2 children
    mid = tree.children_of(tree.root_id)[0]
    grands = tree.children_of(mid.node_id)
    assert len(grands) == 2
    assert grands[0].description == "custom child A"


@pytest.mark.asyncio
async def test_sub_planner_failure_falls_back_to_atomic() -> None:
    """sub_planner raise → 视为 atomic, 不破坏 tree."""

    async def bad_sub_planner(_node: PlanNode):
        raise RuntimeError("LLM down")

    def never_atomic(node: PlanNode) -> bool:
        return node.depth >= 2

    planner = RecursivePlanner(
        max_depth=3, atomic_decider=never_atomic, sub_planner=bad_sub_planner
    )
    tree = await planner.expand(
        root_description="task",
        root_steps=[PlanStepInput(description="step")],
    )
    # 第一层 node 被 sub_planner failed → 标 atomic 兜底
    child = tree.children_of(tree.root_id)[0]
    assert child.is_atomic is True


# ---- PlanTree API ----


@pytest.mark.asyncio
async def test_plantree_walk_dfs_order() -> None:
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="root",
        root_steps=[
            PlanStepInput(description="A", success_criterion="a"),
            PlanStepInput(description="B", success_criterion="b"),
        ],
    )
    descriptions = [n.description for n in tree.walk()]
    # root 先, 然后 A, 然后 B (insertion order)
    assert descriptions[0] == "root"
    assert "A" in descriptions
    assert "B" in descriptions
    # A 比 B 先
    assert descriptions.index("A") < descriptions.index("B")


@pytest.mark.asyncio
async def test_plantree_leaves_only_atomic() -> None:
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="root",
        root_steps=[
            PlanStepInput(description="leaf A", success_criterion="a"),
            PlanStepInput(description="leaf B", success_criterion="b"),
        ],
    )
    leaves = tree.leaves()
    assert all(leaf.is_atomic for leaf in leaves)
    assert len(leaves) == 2


@pytest.mark.asyncio
async def test_plantree_total_estimated_cost_sums_leaves() -> None:
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="root",
        root_steps=[
            PlanStepInput(
                description="A",
                success_criterion="a",
                estimated_cost_usd=0.10,
                estimated_duration_sec=10,
            ),
            PlanStepInput(
                description="B",
                success_criterion="b",
                estimated_cost_usd=0.20,
                estimated_duration_sec=20,
            ),
        ],
    )
    assert tree.total_estimated_cost() == pytest.approx(0.30)
    assert tree.total_estimated_duration() == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_plantree_depth_counts_max() -> None:
    planner = RecursivePlanner(max_depth=2)
    tree = await planner.expand(
        root_description="root",
        root_steps=[PlanStepInput(description="will be recursed long enough")],
    )
    assert tree.depth() == 2


# ---- Validation ----


@pytest.mark.asyncio
async def test_empty_root_description_raises() -> None:
    planner = RecursivePlanner()
    with pytest.raises(ValueError, match="root_description"):
        await planner.expand(
            root_description="   ",
            root_steps=[PlanStepInput(description="a", success_criterion="x")],
        )


# ---- Auto success_criterion for atomic ----


@pytest.mark.asyncio
async def test_atomic_node_gets_default_success_criterion() -> None:
    """User-provided success_criterion=None 但 atomic → 默认生成一个."""
    planner = RecursivePlanner()
    tree = await planner.expand(
        root_description="root",
        root_steps=[
            PlanStepInput(description="short", skill_hint="tool.bash")  # tool prefix → atomic
        ],
    )
    leaf = tree.leaves()[0]
    assert leaf.is_atomic is True
    assert leaf.success_criterion is not None
    assert "完成且可验证" in leaf.success_criterion
