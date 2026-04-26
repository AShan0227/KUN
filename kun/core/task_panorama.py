"""TaskPanorama — 任务全景 (V2.1.2 §2.7 / §13.8).

事前模块统一产出, "作战地图". 12 个事前模块按需展开 (不是档位绑定固定 step).

按需展开矩阵 (V2.1.2 §5.8.1):
- 必跑 (任何任务): task_id + intent_one_sentence
- 按 risk 加跑: 风险预估 / 预冲突 / multi-judge 复审
- 按 complexity 加跑: 拆解 / Context 预热 / 资源预估 / 注意力分配 / 备选路径 / 风险图
- 按 task_type 加跑: 角色实例化
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id

PanoramaTier = Literal["minimal", "light", "medium", "heavy", "full"]


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


__all__ = [
    "AlternativePath",
    "AttentionAllocation",
    "ConflictHint",
    "ContextPreheat",
    "PanoramaPatch",
    "PanoramaTier",
    "PreConflictScan",
    "RiskAssessment",
    "RoleInstance",
    "StepPlan",
    "TaskPanorama",
]
