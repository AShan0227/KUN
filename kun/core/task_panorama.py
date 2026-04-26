"""TaskPanorama — 任务全景 (V2.1.2 §2.7 / §13.8).

事前模块统一产出, "作战地图". 12 个事前模块按需展开 (不是档位绑定固定 step).

按需展开矩阵 (V2.1.2 §5.8.1):
- 必跑 (任何任务): task_id + intent_one_sentence
- 按 risk 加跑: 风险预估 / 预冲突 / multi-judge 复审
- 按 complexity 加跑: 拆解 / Context 预热 / 资源预估 / 注意力分配 / 备选路径 / 风险图
- 按 task_type 加跑: 角色实例化
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from kun.core.ids import new_id

PanoramaTier = Literal["minimal", "light", "medium", "heavy", "full"]
ExecutionMode = Literal["FAST", "SMART", "MAX"]


class ModuleResult(BaseModel):
    """A single on-demand panorama module result."""

    module_name: str
    round_index: int = Field(ge=1, le=3)
    payload: dict[str, Any] = Field(default_factory=dict)
    depth: Literal["minimal", "light", "heavy"] = "minimal"
    required: bool = False


class StepPlan(BaseModel):
    """单 step 执行计划."""

    step_index: int
    skill_id: str | None = None
    role_template_ref: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    estimated_duration_sec: float = 0.0
    intent: str = ""


class RiskAssessment(BaseModel):
    """三维风险预估."""

    financial_risk: float = Field(ge=0.0, le=1.0, default=0.0)
    irreversibility_risk: float = Field(ge=0.0, le=1.0, default=0.0)
    complexity_risk: float = Field(ge=0.0, le=1.0, default=0.0)
    overall_risk_level: Literal["low", "medium", "high", "critical"] = "medium"


class ConflictHint(BaseModel):
    """冲突预警."""

    resource: str
    conflict_kind: Literal["resource_lock", "version_mismatch", "side_effect_overlap"]
    related_task_ids: list[str] = Field(default_factory=list)


class PreConflictScan(BaseModel):
    """预冲突扫描结果."""

    conflicts_found: list[ConflictHint] = Field(default_factory=list)
    resolution: Literal["delay", "serialize", "merge", "no_conflict"] = "no_conflict"


class ContextPreheat(BaseModel):
    """Context 预热产出."""

    pinned_assets: list[str] = Field(default_factory=list)
    semantic_top_k: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)
    capability_card_snapshot: dict[str, Any] = Field(default_factory=dict)
    depth: Literal["shallow", "deep"] = "shallow"


class AttentionAllocation(BaseModel):
    """注意力分配 (V1 §7.4 5 维公式)."""

    importance: float = Field(ge=0.0, le=1.0, default=0.0)
    complexity: float = Field(ge=0.0, le=1.0, default=0.0)
    urgency: float = Field(ge=0.0, le=1.0, default=0.0)
    surprise: float = Field(ge=0.0, le=1.0, default=0.0)
    risk: float = Field(ge=0.0, le=1.0, default=0.0)
    overall_score: float = Field(ge=0.0, le=1.0, default=0.0)
    chosen_model_tier: str = "main"
    chosen_evaluation_tier: int = 0
    chosen_sandbox_tier: str = "硬化容器"


class RoleInstance(BaseModel):
    """角色实例化产出."""

    role_template_ref: str
    instance_id: str
    capability_card_ref: str | None = None
    assigned_steps: list[int] = Field(default_factory=list)


class AlternativePath(BaseModel):
    """备选路径 (涌现切换候选)."""

    path_id: str
    description: str
    estimated_cost_usd: float
    estimated_duration_sec: float
    rejected_reason: str | None = None


class PanoramaPatch(BaseModel):
    """DAG 热修改历史 (§7.7)."""

    patch_id: str
    patched_at: datetime
    reason: Literal[
        "emergent_solution_swap", "user_correction", "watchtower_intervention", "ooda_replan"
    ]
    patch_kind: Literal["node_replace", "node_insert", "node_delete", "subgraph_replace"]
    affected_nodes: list[int] = Field(default_factory=list)
    notes: str = ""


class TaskPanorama(BaseModel):
    """任务全景."""

    panorama_id: str = Field(default_factory=lambda: new_id("tp"))
    task_ref: str
    tier: PanoramaTier
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    generated_in_ms: int = 0
    generator_version: str = "v2.1.2"

    # 极简档以上必填
    intent_one_sentence: str = ""
    audience: Literal["novice", "developer", "expert"] = "developer"
    chosen_template_ref: str | None = None

    # 轻档以上加
    execution_plan: list[StepPlan] = Field(default_factory=list)
    estimated_total_cost_usd: float = 0.0
    estimated_total_duration_sec: float = 0.0
    estimated_total_tokens: int = 0

    # 标准档以上加
    context_preheat: ContextPreheat | None = None
    risk_assessment: RiskAssessment | None = None
    pre_conflict_scan: PreConflictScan | None = None
    attention_allocation: AttentionAllocation | None = None
    role_instances: list[RoleInstance] = Field(default_factory=list)

    # 完整档加
    alternative_paths: list[AlternativePath] = Field(default_factory=list)
    multi_judge_review: dict[str, Any] | None = None

    # 全档共有
    patches: list[PanoramaPatch] = Field(default_factory=list)
    modules_run: list[str] = Field(default_factory=list)
    modules_skipped: list[str] = Field(default_factory=list)

    async def build_anchored(
        self,
        task_ref: Any | None = None,
        *,
        execution_mode: ExecutionMode | None = None,
        max_rounds: int | None = None,
    ) -> AsyncIterator[ModuleResult]:
        """Yield panorama modules in anchor-then-expand rounds.

        Round 1 is the always-on minimal anchor. Round 2 adds light risk and
        complexity modules. Round 3 adds heavy review modules. Callers may stop
        consuming at any point, including future marginal-ROI decisions.

        TODO: wire by Claude in V2.2.
        """

        ref = task_ref if task_ref is not None else self
        mode = (
            execution_mode or _execution_mode_from_ref(ref) or _execution_mode_from_tier(self.tier)
        )
        mode_rounds = _rounds_for_execution_mode(mode)
        if max_rounds is not None:
            if max_rounds < 1:
                raise ValueError("max_rounds must be >= 1")
            mode_rounds = min(mode_rounds, max_rounds)
        effective_rounds = min(mode_rounds, 3)

        for module in _modules_for_rounds(effective_rounds):
            yield _build_module_result(module, ref, self)


__all__ = [
    "AlternativePath",
    "AttentionAllocation",
    "ConflictHint",
    "ContextPreheat",
    "ExecutionMode",
    "ModuleResult",
    "PanoramaPatch",
    "PanoramaTier",
    "PreConflictScan",
    "RiskAssessment",
    "RoleInstance",
    "StepPlan",
    "TaskPanorama",
]


def _execution_mode_from_tier(tier: PanoramaTier) -> ExecutionMode:
    if tier in ("minimal", "light"):
        return "FAST"
    if tier in ("medium", "heavy"):
        return "SMART"
    return "MAX"


def _execution_mode_from_ref(task_ref: Any) -> ExecutionMode | None:
    mode = _ref_value(task_ref, "execution_mode")
    if mode in ("FAST", "SMART", "MAX"):
        return cast(ExecutionMode, mode)
    return None


def _rounds_for_execution_mode(mode: ExecutionMode) -> int:
    return {"FAST": 1, "SMART": 2, "MAX": 3}[mode]


def _modules_for_rounds(
    max_rounds: int,
) -> list[tuple[str, int, Literal["minimal", "light", "heavy"]]]:
    modules: list[tuple[str, int, Literal["minimal", "light", "heavy"]]] = [
        ("intent_one_sentence", 1, "minimal"),
        ("risk_summary", 1, "minimal"),
    ]
    if max_rounds >= 2:
        modules.extend(
            [
                ("risk_assessment", 2, "light"),
                ("complexity_score", 2, "light"),
            ]
        )
    if max_rounds >= 3:
        modules.extend(
            [
                ("multi_judge_review", 3, "heavy"),
                ("cross_check", 3, "heavy"),
                ("alternative_paths", 3, "heavy"),
                ("risk_graph", 3, "heavy"),
            ]
        )
    return modules


def _build_module_result(
    module: tuple[str, int, Literal["minimal", "light", "heavy"]],
    task_ref: Any,
    panorama: TaskPanorama,
) -> ModuleResult:
    name, round_index, depth = module
    payload_builders: dict[str, Callable[[], dict[str, Any]]] = {
        "intent_one_sentence": lambda: {
            "task_ref": _task_id(task_ref, panorama),
            "intent_one_sentence": _intent(task_ref, panorama),
        },
        "risk_summary": lambda: {
            "task_ref": _task_id(task_ref, panorama),
            "risk_level": _ref_value(task_ref, "risk_level", "low"),
        },
        "risk_assessment": lambda: {
            "risk_level": _ref_value(task_ref, "risk_level", "low"),
            "estimated_cost_usd": _ref_value(task_ref, "estimated_cost_usd", 0.0),
        },
        "complexity_score": lambda: {
            "complexity_score": _ref_value(task_ref, "complexity_score", 0.0),
        },
        "multi_judge_review": lambda: {
            "enabled": True,
            "reason": "heavy panorama expansion",
        },
        "cross_check": lambda: {
            "enabled": True,
            "reason": "heavy panorama expansion",
        },
        "alternative_paths": lambda: {
            "enabled": True,
            "reason": "heavy panorama expansion",
        },
        "risk_graph": lambda: {
            "enabled": True,
            "reason": "heavy panorama expansion",
        },
    }
    return ModuleResult(
        module_name=name,
        round_index=round_index,
        depth=depth,
        required=round_index == 1,
        payload=payload_builders[name](),
    )


def _task_id(task_ref: Any, panorama: TaskPanorama) -> str:
    value = _ref_value(task_ref, "task_id")
    if value is None and isinstance(task_ref, str):
        value = task_ref
    if value is None:
        value = panorama.task_ref
    return str(value)


def _intent(task_ref: Any, panorama: TaskPanorama) -> str:
    value = _ref_value(task_ref, "intent_one_sentence")
    if value is None:
        value = _ref_value(task_ref, "success_criteria_short")
    if value is None:
        value = _ref_value(task_ref, "user_message")
    if value is None:
        value = panorama.intent_one_sentence
    return str(value or "(no explicit intent)")


def _ref_value(task_ref: Any, key: str, default: Any = None) -> Any:
    if isinstance(task_ref, dict):
        if key in task_ref:
            return task_ref[key]
        meta = task_ref.get("meta")
        if isinstance(meta, dict) and key in meta:
            return meta[key]
        if meta is not None and hasattr(meta, key):
            return getattr(meta, key)
        return default

    if hasattr(task_ref, key):
        return getattr(task_ref, key)

    meta = getattr(task_ref, "meta", None)
    if meta is not None and hasattr(meta, key):
        return getattr(meta, key)
    return default
