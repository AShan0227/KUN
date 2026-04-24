"""Handoff Protocol (§13.3) — 跨角色交接的 L1-L4 包结构.

默认只带 L1 + L2; L3 / L4 按需拉取 (对象存储引用).

ADR-005 (Outbox): Handoff 写入时同时写 events 表, 消费者通过 NATS 通知拉取.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id
from kun.datamodel.capability import EntityRef

SerializationFormat = Literal["json", "toon", "msgpack"]


class BudgetRemaining(BaseModel):
    """预算剩余快照."""

    usd: float = 0.0
    time_seconds: float = 0.0
    llm_calls: int = 0


class KnownRisk(BaseModel):
    description: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    mitigation_hint: str | None = None


class CapabilitySnapshot(BaseModel):
    """嵌入 L2 的能力卡切片, 作"诚实通讯"的一部分 (§8.4)."""

    task_type: str
    historical_success_rate: float = Field(ge=0.0, le=1.0)
    sample_size_effective: int = 0


class HandoffL1(BaseModel):
    """L1: 任务核心, 必带, ≤ 500 tokens."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(default_factory=lambda: new_id("handoff"))
    from_entity: EntityRef
    to_entity: EntityRef
    task_ref: str = Field(description="tk-... TASK.md id")
    timestamp: datetime

    intent_one_sentence: str = Field(max_length=300)
    deliverable_required: str = Field(max_length=300)
    deadline_iso: datetime | None = None

    budget_remaining: BudgetRemaining = Field(default_factory=BudgetRemaining)
    authorization_scope: list[str] = Field(default_factory=list)

    runtime_state_ref: str | None = Field(default=None, description="rs-... RuntimeState id")


class HandoffL2(BaseModel):
    """L2: 上游假设 + 风险, 关键场景必带, ≤ 2000 tokens."""

    model_config = ConfigDict(extra="forbid")

    upstream_assumptions: list[str] = Field(default_factory=list)
    known_risks: list[KnownRisk] = Field(default_factory=list)
    upstream_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    consistency_score: float | None = Field(default=None, ge=0.0, le=1.0)
    capability_card_snapshot: CapabilitySnapshot | None = None
    recommendation: str | None = None


class HandoffL3(BaseModel):
    """L3: 推理链, 按需加载 (对象存储引用形式)."""

    model_config = ConfigDict(extra="forbid")

    reasoning_trace_ref: str = Field(description="s3://... ref to full trace JSON")
    considered_alternatives_ref: str | None = None


class HandoffL4(BaseModel):
    """L4: 完整产物, 按需加载."""

    model_config = ConfigDict(extra="forbid")

    artifact_refs: list[dict[str, str]] = Field(
        default_factory=list,
        description="List of {type, ref}",
    )


class HandoffPacket(BaseModel):
    """Full packet with all layers.

    默认 serialize 时只输出 L1+L2; L3/L4 用 refs 指回对象存储.
    """

    model_config = ConfigDict(extra="forbid")

    l1: HandoffL1
    l2: HandoffL2 | None = None
    l3: HandoffL3 | None = None
    l4: HandoffL4 | None = None
    serialization: SerializationFormat = "json"

    def compact(self) -> dict[str, Any]:
        """Emit only L1+L2 as dict (default over-the-wire form)."""
        data: dict[str, Any] = {"l1": self.l1.model_dump(mode="json")}
        if self.l2 is not None:
            data["l2"] = self.l2.model_dump(mode="json")
        return data

    def full(self) -> dict[str, Any]:
        """Emit all layers as dict (for debugging / audit)."""
        return self.model_dump(mode="json")
