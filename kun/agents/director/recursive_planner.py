"""Recursive task planner (LT.F).

Existing `TaskPlanner` 输出 flat PlanStep list (1 层). 真正复杂任务 (estimated_steps>5,
risk=high) 单层不够 — 用户 "implement OAuth login" 拆成 5 步, 但每步 (e.g.
"write JWT decoder") 自己也是个小任务需要 sub-steps.

RecursivePlanner 在 TaskPlanner 输出基础上构造 PlanTree:
  - root_node (task 本身)
  - 1st-level children = TaskPlanner.plan() 的 PlanStep
  - 每个 child 由 atomic_decider 判定 "需要再拆吗?":
      atomic_decider → True  → leaf (is_atomic=True), 标 success_criterion
      atomic_decider → False → sub_planner 产生 child nodes, 递归
  - 限制 max_depth + max_breadth_per_node 防爆炸
  - 每个 node 有 verification_hint 供 ExecutorLoop / Tester 独立验证

设计点:
  - sub_planner 和 atomic_decider 全 DI: 默认规则版 (启发式), 真生产替换 LLM-based
  - PlanTree frozen + 不可变 — caller 用 walk() / leaves() / children_of() 遍历
  - estimated_cost / estimated_duration 自底向上聚合 (sum of leaves)

为什么不改 TaskPlanner 直接出 tree:
  - TaskPlanner 现在用得多 (orchestrator/tests), 改它撞回归
  - 递归是 opt-in: 短任务不需要, complex 长任务才用
  - 解耦: RecursivePlanner 不依赖 TaskPlanner 实现, 只接 step list 入参
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.director.recursive_planner")


# ---- Data types ----


@dataclass(frozen=True)
class PlanNode:
    """递归计划树节点 — frozen, 不可变."""

    node_id: str
    description: str
    depth: int  # 0 = root, 1 = top-level step, etc.
    parent_id: str | None
    skill_hint: str | None = None
    estimated_cost_usd: float = 0.0
    estimated_duration_sec: float = 0.0
    is_atomic: bool = False
    children_ids: list[str] = field(default_factory=list)
    # Atomic 才有这些字段
    success_criterion: str | None = None
    verification_hint: str | None = None
    # 自由扩展槽
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanTree:
    """完整的 plan tree, by node_id 索引."""

    nodes: dict[str, PlanNode]
    root_id: str

    def root(self) -> PlanNode:
        return self.nodes[self.root_id]

    def children_of(self, node_id: str) -> list[PlanNode]:
        return [self.nodes[c] for c in self.nodes[node_id].children_ids]

    def leaves(self) -> list[PlanNode]:
        return [n for n in self.nodes.values() if n.is_atomic]

    def walk(self, *, node_id: str | None = None) -> Iterator[PlanNode]:
        """DFS 遍历 — root → children → grandchildren ..."""
        start_id = node_id or self.root_id
        stack = [start_id]
        while stack:
            cur = stack.pop()
            node = self.nodes[cur]
            yield node
            # children 反序入 stack, 使 DFS 输出按 insertion order
            stack.extend(reversed(node.children_ids))

    def depth(self) -> int:
        return max((n.depth for n in self.nodes.values()), default=0)

    def node_count(self) -> int:
        return len(self.nodes)

    def total_estimated_cost(self) -> float:
        """leaves 的 cost 累加."""
        return sum(n.estimated_cost_usd for n in self.leaves())

    def total_estimated_duration(self) -> float:
        return sum(n.estimated_duration_sec for n in self.leaves())


# ---- Callback types (DI) ----

AtomicDecider = Callable[[PlanNode], bool]
"""(node) → True 当 node 不应再拆 (是原子可执行单元)."""

SubPlanner = Callable[[PlanNode], Awaitable[list["PlanStepInput"]]]
"""(node) → list of (description, skill_hint, est_cost, est_dur) for children.

子规划器: 给一个 non-atomic 节点, 产生它的 children. 真生产用 LLM 拆解,
测试 / dev 用规则版.
"""


@dataclass(frozen=True)
class PlanStepInput:
    """SubPlanner 返给 RecursivePlanner 的简单输入 (RecursivePlanner 自分配 id+depth)."""

    description: str
    skill_hint: str | None = None
    estimated_cost_usd: float = 0.0
    estimated_duration_sec: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    success_criterion: str | None = None
    """提供时, atomic_decider 倾向于把它视为 atomic (有具体成功标准)."""


# ---- Default heuristics (no LLM) ----


def _default_atomic_decider(node: PlanNode) -> bool:
    """规则兜底 atomic_decider — 无 LLM 时也可用.

    Atomic when:
      - 有具体 success_criterion (用户/sub_planner 写明白了)
      - depth >= 2 (默认深度限, 防无限递归)
      - description ≤ 12 chars (短描述通常已经原子)
      - skill_hint 形如 'tool.x' (具体 tool 直接调即原子)
    """
    if node.success_criterion:
        return True
    if node.depth >= 2:
        return True
    if len(node.description.strip()) <= 12:
        return True
    return bool(node.skill_hint and node.skill_hint.startswith("tool."))


async def _default_sub_planner(node: PlanNode) -> list[PlanStepInput]:
    """规则兜底 sub_planner — 把 node 拆成"先准备 → 跑 → 验证"3 步.

    真生产用 LLM-based sub_planner 替换 (e.g. 引导问'你要怎么完成 {desc}? 列 3 步').
    """
    base = node.description.strip()
    if len(base) > 60:
        base = base[:57] + "..."
    return [
        PlanStepInput(
            description=f"准备: {base} 所需输入/上下文",
            skill_hint=node.skill_hint,
            estimated_cost_usd=node.estimated_cost_usd / 3 if node.estimated_cost_usd else 0.0,
            estimated_duration_sec=node.estimated_duration_sec / 3
            if node.estimated_duration_sec
            else 0.0,
        ),
        PlanStepInput(
            description=f"执行: {base}",
            skill_hint=node.skill_hint,
            estimated_cost_usd=node.estimated_cost_usd / 3 if node.estimated_cost_usd else 0.0,
            estimated_duration_sec=node.estimated_duration_sec / 3
            if node.estimated_duration_sec
            else 0.0,
        ),
        PlanStepInput(
            description=f"验证: {base} 完成",
            success_criterion=f"{base} 的可验证结果存在",
            estimated_cost_usd=node.estimated_cost_usd / 3 if node.estimated_cost_usd else 0.0,
            estimated_duration_sec=node.estimated_duration_sec / 3
            if node.estimated_duration_sec
            else 0.0,
        ),
    ]


# ---- Planner ----


class RecursivePlanner:
    """递归拆解器 — 在 flat PlanSteps 之上递归产 PlanTree."""

    def __init__(
        self,
        *,
        max_depth: int = 3,
        max_breadth_per_node: int = 8,
        atomic_decider: AtomicDecider | None = None,
        sub_planner: SubPlanner | None = None,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if max_breadth_per_node < 1:
            raise ValueError("max_breadth_per_node must be >= 1")
        self._max_depth = max_depth
        self._max_breadth = max_breadth_per_node
        self._atomic_decider = atomic_decider or _default_atomic_decider
        self._sub_planner = sub_planner or _default_sub_planner

    async def expand(
        self,
        *,
        root_description: str,
        root_steps: list[PlanStepInput],
    ) -> PlanTree:
        """Build PlanTree from root description + flat top-level steps.

        root_steps 是 caller (通常 TaskPlanner.plan().steps) 提供的第一层.
        每个 step 由 atomic_decider 决定是否再拆, 不 atomic 的调 sub_planner 递归.

        Returns:
          PlanTree — root + 所有 descendants, by node_id 索引.
        """
        if not root_description.strip():
            raise ValueError("root_description must be non-empty")

        nodes: dict[str, PlanNode] = {}

        root_id = new_id("task_checkpoint")  # 复用 entity prefix; tree id 临时
        root_children: list[str] = []
        # Build root first (will set children_ids below)
        # ...
        # We'll handle children first then re-build root with children_ids since
        # PlanNode is frozen.

        # Process top-level
        for step in root_steps[: self._max_breadth]:
            child = self._build_node(
                step=step, depth=1, parent_id=root_id
            )
            nodes[child.node_id] = child
            root_children.append(child.node_id)
            await self._maybe_recurse(child, nodes, depth=1)

        # Finalize root with children_ids
        root_node = PlanNode(
            node_id=root_id,
            description=root_description.strip(),
            depth=0,
            parent_id=None,
            is_atomic=False,
            children_ids=root_children,
        )
        nodes[root_id] = root_node

        log.info(
            "recursive_planner.tree_built",
            root_id=root_id,
            node_count=len(nodes),
            leaves=sum(1 for n in nodes.values() if n.is_atomic),
            depth=max(n.depth for n in nodes.values()),
        )
        return PlanTree(nodes=nodes, root_id=root_id)

    def _build_node(
        self,
        *,
        step: PlanStepInput,
        depth: int,
        parent_id: str | None,
    ) -> PlanNode:
        node_id = new_id("task_checkpoint")
        # Provisional is_atomic — will be re-evaluated by decider below
        return PlanNode(
            node_id=node_id,
            description=step.description,
            depth=depth,
            parent_id=parent_id,
            skill_hint=step.skill_hint,
            estimated_cost_usd=step.estimated_cost_usd,
            estimated_duration_sec=step.estimated_duration_sec,
            is_atomic=False,  # set later
            children_ids=[],
            success_criterion=step.success_criterion,
            metadata=dict(step.metadata),
        )

    async def _maybe_recurse(
        self,
        node: PlanNode,
        nodes: dict[str, PlanNode],
        *,
        depth: int,
    ) -> None:
        """决定 node 是否原子; 否则展开 children + 递归."""
        # 评估 atomicity
        is_atomic = self._atomic_decider(node)
        if depth >= self._max_depth:
            # 强制 atomic 防爆炸
            is_atomic = True

        if is_atomic:
            # Replace node with is_atomic=True
            updated = PlanNode(
                node_id=node.node_id,
                description=node.description,
                depth=node.depth,
                parent_id=node.parent_id,
                skill_hint=node.skill_hint,
                estimated_cost_usd=node.estimated_cost_usd,
                estimated_duration_sec=node.estimated_duration_sec,
                is_atomic=True,
                children_ids=[],
                success_criterion=node.success_criterion
                or _default_success_criterion(node.description),
                verification_hint=node.verification_hint,
                metadata=dict(node.metadata),
            )
            nodes[node.node_id] = updated
            return

        # Non-atomic → sub_planner 产 children
        try:
            child_inputs = await self._sub_planner(node)
        except Exception as e:
            log.warning(
                "recursive_planner.sub_planner_failed_using_atomic",
                node_id=node.node_id,
                error=str(e),
            )
            # 失败兜底: 当 atomic
            nodes[node.node_id] = PlanNode(
                node_id=node.node_id,
                description=node.description,
                depth=node.depth,
                parent_id=node.parent_id,
                skill_hint=node.skill_hint,
                estimated_cost_usd=node.estimated_cost_usd,
                estimated_duration_sec=node.estimated_duration_sec,
                is_atomic=True,
                children_ids=[],
                success_criterion=_default_success_criterion(node.description),
                metadata=dict(node.metadata),
            )
            return

        child_ids: list[str] = []
        for cinput in child_inputs[: self._max_breadth]:
            child = self._build_node(
                step=cinput, depth=depth + 1, parent_id=node.node_id
            )
            nodes[child.node_id] = child
            child_ids.append(child.node_id)
            await self._maybe_recurse(child, nodes, depth=depth + 1)

        # Update parent node with children_ids
        nodes[node.node_id] = PlanNode(
            node_id=node.node_id,
            description=node.description,
            depth=node.depth,
            parent_id=node.parent_id,
            skill_hint=node.skill_hint,
            estimated_cost_usd=node.estimated_cost_usd,
            estimated_duration_sec=node.estimated_duration_sec,
            is_atomic=False,
            children_ids=child_ids,
            metadata=dict(node.metadata),
        )


def _default_success_criterion(description: str) -> str:
    """节点没有 user-provided success_criterion 时, 启发式生成一个."""
    snippet = description.strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    return f"完成且可验证: {snippet}"


__all__ = [
    "AtomicDecider",
    "PlanNode",
    "PlanStepInput",
    "PlanTree",
    "RecursivePlanner",
    "SubPlanner",
]
