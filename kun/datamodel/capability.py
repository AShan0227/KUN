"""能力卡 (Capability Card) 数据模型 (§13.2).

两类能力卡严格分开 (ADR):
  model_能力卡:        供路由层查, 粒度 模型×任务类型
  role_template_能力卡: 供任务分配层查, 粒度 角色模板×任务类型
  human / external_agent / company: 协作调度器使用

冷启动三态: cold_start → warming_up → mature
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kun.core.ids import new_id
from kun.core.scoring import ScoreDescriptor, wilson_ci95

EntityType = Literal[
    "role_template",
    "human",
    "external_agent",
    "company",
    "model",
]

Maturity = Literal["cold_start", "warming_up", "mature"]


class EntityRef(BaseModel):
    """指向一个协作实体."""

    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    entity_id: str

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id}"


class Stats(BaseModel):
    """历史表现统计."""

    total_invocations: int = 0
    success_count: int = 0
    partial_success_count: int = 0
    failure_count: int = 0
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    success_rate_ci95: tuple[float, float] | None = None
    avg_cost_usd: float = 0.0
    avg_duration_sec: float = 0.0
    duration_p50: float = 0.0
    duration_p95: float = 0.0
    duration_p99: float = 0.0

    def recompute_rate(self) -> None:
        """Update success_rate + Wilson CI in place based on counts."""
        # partial success counts as 0.5 for rate purposes
        weighted = self.success_count + 0.5 * self.partial_success_count
        self.success_rate = weighted / self.total_invocations if self.total_invocations else 0.0
        self.success_rate_ci95 = wilson_ci95(self.success_count, self.total_invocations)


class QualityMetrics(BaseModel):
    """质量维度."""

    avg_rubric_score: float = Field(default=0.0, ge=0.0, le=5.0)
    consistency_score: float = Field(default=0.0, ge=0.0, le=1.0)
    surprise_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    last_benchmark_score: float = Field(default=0.0, ge=0.0, le=1.0)


class FailureMode(BaseModel):
    """一种失败模式的分布."""

    name: str
    frequency: int = 0
    last_occurred: datetime | None = None
    typical_root_cause: str = ""


class DecayModel(BaseModel):
    """统计的衰减 (半衰期)."""

    half_life_days: int = Field(default=30, gt=0)
    last_decay_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_sample_size: float = 0.0

    def decay_weight(self, event_time: datetime, at: datetime | None = None) -> float:
        """Return the weight of an event that occurred at event_time."""
        now = at or datetime.now(UTC)
        elapsed = (now - event_time).total_seconds() / 86400
        if elapsed <= 0:
            return 1.0
        return math.exp(-math.log(2) * elapsed / self.half_life_days)


class Boundaries(BaseModel):
    """写给调度方看的能力边界."""

    recommended_max_complexity: float = Field(default=1.0, ge=0.0, le=1.0)
    not_recommended_for: list[str] = Field(default_factory=list)
    require_escalation_for: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    """一条具体的能力记录 (某一 task_type 上的表现)."""

    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(description="Hierarchical category matching TaskMeta.task_type")
    short_description: str = ""
    stats: Stats = Field(default_factory=Stats)
    quality: QualityMetrics = Field(default_factory=QualityMetrics)
    failure_modes: list[FailureMode] = Field(default_factory=list)
    decay: DecayModel = Field(default_factory=DecayModel)
    boundaries: Boundaries = Field(default_factory=Boundaries)

    def capability_score(self) -> ScoreDescriptor:
        """Produce a unified ScoreDescriptor for this capability.

        The router layer queries this to rank candidates on a task_type.
        """
        # Blend success rate + quality + consistency, weighted.
        components = {
            "success": self.stats.success_rate,
            "quality": self.quality.avg_rubric_score / 5.0,  # normalize 0-5 → 0-1
            "consistency": self.quality.consistency_score,
        }
        weights = {"success": 0.5, "quality": 0.3, "consistency": 0.2}
        return ScoreDescriptor.compose(
            kind="capability",
            components=components,
            weights=weights,
            sample_size=int(self.decay.effective_sample_size),
            half_life_days=self.decay.half_life_days,
        )


class CapabilityCard(BaseModel):
    """实体 × 多个任务类型的能力画像."""

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(default_factory=lambda: new_id("capability"))
    entity_ref: EntityRef
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capabilities: list[Capability] = Field(default_factory=list)
    primary_strength: str | None = None
    primary_weakness: str | None = None
    overall_reliability: float = Field(default=0.0, ge=0.0, le=1.0)
    maturity: Maturity = "cold_start"

    @field_validator("capabilities")
    @classmethod
    def _unique_task_types(cls, v: list[Capability]) -> list[Capability]:
        seen: set[str] = set()
        for cap in v:
            if cap.task_type in seen:
                raise ValueError(f"Duplicate task_type in capabilities: {cap.task_type}")
            seen.add(cap.task_type)
        return v

    # ---- lookup helpers ----

    def find(self, task_type: str) -> Capability | None:
        """Exact task_type match."""
        for cap in self.capabilities:
            if cap.task_type == task_type:
                return cap
        return None

    def find_best_match(self, task_type: str) -> Capability | None:
        """Find the most specific capability matching task_type (walk up hierarchy).

        For "coding.python.fastapi" try:
          coding.python.fastapi → coding.python → coding
        """
        parts = task_type.split(".")
        for depth in range(len(parts), 0, -1):
            candidate = ".".join(parts[:depth])
            cap = self.find(candidate)
            if cap is not None:
                return cap
        return None

    def recompute_summary(self) -> None:
        """Refresh primary_strength / weakness / overall_reliability / maturity."""
        if not self.capabilities:
            self.overall_reliability = 0.0
            self.maturity = "cold_start"
            return

        scored = [(c.task_type, c.capability_score().value) for c in self.capabilities]
        scored.sort(key=lambda x: x[1], reverse=True)
        self.primary_strength = scored[0][0]
        self.primary_weakness = scored[-1][0]

        total_samples = sum(c.decay.effective_sample_size for c in self.capabilities)
        if total_samples > 0:
            weighted = sum(
                c.capability_score().value * c.decay.effective_sample_size
                for c in self.capabilities
            )
            self.overall_reliability = weighted / total_samples
        else:
            self.overall_reliability = sum(s for _, s in scored) / len(scored)

        # Maturity: cold_start <50 samples, warming_up <200, mature >=200
        if total_samples < 50:
            self.maturity = "cold_start"
        elif total_samples < 200:
            self.maturity = "warming_up"
        else:
            self.maturity = "mature"

        self.last_updated = datetime.now(UTC)
