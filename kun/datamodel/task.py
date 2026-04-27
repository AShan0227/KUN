"""TASK.md 数据模型 (§13.1).

三级渐进披露:
  Layer 1 = TaskMeta (≤ 80 tokens, 始终在 context)
  Layer 2 = TaskSpec (≤ 2000 tokens, 执行蓝图, 按需加载)
  Layer 3 = 完整上下文 (无大小限制, 真执行时加载) — 通过 TaskRef 拉取

Key design:
  - task_id 是 ULID (可排序)
  - fingerprint 是 hash(description + owner + time_window) 作为幂等键
  - task_type 是层级分类 "coding.python.fastapi" → 支持向上聚合
  - risk_level 驱动评估矩阵 (§8.1)
  - 运行时状态不在 TASK.md 里, 单独 RuntimeState (§13.4)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kun.context.assets import AssetLayer, LayeredAsset
from kun.core.ids import new_id
from kun.datamodel.verification_spec import VerificationSpec

RiskLevel = Literal["low", "medium", "high", "critical"]
ExecutionMode = Literal["FAST", "SMART", "MAX"]


class Owner(BaseModel):
    """TASK.md owner block."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None


class TaskMeta(BaseModel):
    """Layer 1: 始终在 context, 极小元数据.

    目标 ≤ 80 tokens 序列化后.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=lambda: new_id("task"))
    fingerprint: str
    task_type: str = Field(description="Hierarchical category, e.g. 'coding.python.fastapi'")
    risk_level: RiskLevel = "low"
    complexity_score: float = Field(default=0.3, ge=0.0, le=1.0)
    owner: Owner
    estimated_cost_usd: float = Field(default=0.05, ge=0.0)
    estimated_duration_sec: float = Field(default=30.0, ge=0.0)
    execution_mode: ExecutionMode = "FAST"
    mode_override_reason: str = ""
    deadline_iso: datetime | None = None
    success_criteria_short: str = Field(max_length=200)
    version: int = Field(default=1, description="TASK.md structure version, not run count")

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, v: str) -> str:
        # Must be dotted lowercase segments
        parts = v.split(".")
        if not parts or not all(p and p.replace("_", "").replace("-", "").isalnum() for p in parts):
            raise ValueError(f"task_type must be dotted.lowercase.segments, got {v!r}")
        return v.lower()

    @field_validator("fingerprint")
    @classmethod
    def _validate_fp(cls, v: str) -> str:
        if not v.startswith("sha256:"):
            raise ValueError("fingerprint must start with 'sha256:'")
        if len(v) != len("sha256:") + 64:
            raise ValueError("fingerprint must be sha256:<64 hex>")
        return v

    @classmethod
    def compute_fingerprint(
        cls,
        description: str,
        owner: Owner,
        *,
        time_window_min: int = 5,
    ) -> str:
        """Compute a stable fingerprint for idempotency.

        Within time_window_min, identical task descriptions from same owner
        get the same fingerprint → dedup.
        """
        bucket = int(datetime.now(UTC).timestamp() // (time_window_min * 60))
        payload = f"{description}|{owner.tenant_id}|{owner.user_id or ''}|{bucket}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


class Constraint(BaseModel):
    """A single execution constraint."""

    kind: Literal["no_external_paid_api", "path_only", "budget_cap", "no_irreversible", "custom"]
    detail: str


class Risk(BaseModel):
    """A foreseen risk + mitigation."""

    description: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    mitigation_hint: str | None = None


class TaskSpec(BaseModel):
    """Layer 2: 执行蓝图. 按需加载. ≤ 2000 tokens 序列化后.

    这一层由意图理解 + 大模型产出, 工程层校验补全.
    """

    model_config = ConfigDict(extra="forbid")

    goal_detail: str = Field(description="具体可验证的目标描述")
    success_metrics: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    subtasks_hint: list[str] = Field(default_factory=list)
    external_resources: list[str] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    foreseen_risks: list[Risk] = Field(default_factory=list)
    fallback_plan: str | None = None
    parent_task_id: str | None = None
    blocking_task_ids: list[str] = Field(default_factory=list)
    # V2.2 Wire 36 (BATCH4 C3 / T53): 任务完成验证规格 — orchestrator 标记 done 前
    # 跑 VerificationRunner.verify(), 任何 required spec failed → mark failed
    verification_specs: list[VerificationSpec] = Field(default_factory=list)


class TaskLayer3Context(BaseModel):
    """Layer 3: full execution context, loaded only when truly needed."""

    model_config = ConfigDict(extra="forbid")

    project_context: str = ""
    user_context: str = ""
    historical_notes: list[str] = Field(default_factory=list)
    asset_refs: list[str] = Field(default_factory=list)
    raw_input_ref: str | None = None

    def summary(self, *, max_chars: int = 1200) -> str:
        parts = [
            self.project_context,
            self.user_context,
            "\n".join(self.historical_notes),
            f"assets: {', '.join(self.asset_refs)}" if self.asset_refs else "",
            f"raw_input_ref: {self.raw_input_ref}" if self.raw_input_ref else "",
        ]
        text = "\n".join(part for part in parts if part).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20].rstrip() + "\n...<truncated>"


class TaskRef(BaseModel):
    """引用一个完整任务 (L1 + 可选 L2 / L3 引用)."""

    meta: TaskMeta
    spec: TaskSpec | None = None
    layer3_context: TaskLayer3Context | None = None
    layer3_ref: str | None = Field(
        default=None,
        description="对象存储引用 (s3://...) 或内部 asset id (mm-xxx)",
    )

    def l1_summary(self) -> str:
        """Compact one-liner for logs / notifications."""
        return (
            f"{self.meta.task_id} [{self.meta.task_type}/{self.meta.risk_level}] "
            f"{self.meta.success_criteria_short}"
        )

    def to_layered_asset(self, *, layer: AssetLayer = AssetLayer.L1_TASK) -> LayeredAsset:
        """Expose TASK.md through the same progressive-disclosure asset interface."""

        metadata = {
            "task_id": self.meta.task_id,
            "task_type": self.meta.task_type,
            "risk_level": self.meta.risk_level,
            "complexity_score": self.meta.complexity_score,
            "execution_mode": self.meta.execution_mode,
            "success_criteria_short": self.meta.success_criteria_short,
            "project_id": self.meta.owner.project_id or "",
        }
        summary_parts = [self.meta.success_criteria_short]
        if self.spec is not None:
            summary_parts.extend(
                [
                    self.spec.goal_detail,
                    f"success_metrics={self.spec.success_metrics}",
                    f"required_skills={self.spec.required_skills}",
                    f"required_tools={self.spec.required_tools}",
                ]
            )
        if self.layer3_context is not None:
            summary_parts.append(self.layer3_context.summary(max_chars=600))
        full_ref = self.layer3_ref or (
            f"task_l3://{self.meta.task_id}" if self.layer3_context is not None else None
        )
        return LayeredAsset.build(
            "task",
            self.meta.owner.tenant_id,
            metadata=metadata,
            summary="\n".join(part for part in summary_parts if part),
            full_ref=full_ref,
            layer=layer,
            tags=[self.meta.task_type, self.meta.risk_level, self.meta.execution_mode],
        )
