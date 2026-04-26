"""Soul File — 灵魂档案 (V2.1 §13.6 + §20.4 / T17 + T44).

user 级、append-only、governance 严格的"用户驱动分化"载体.

Governance 规则 (§12.8):
- append-only 历史 (修改全部走 append, 不可篡改原条目)
- 写入需 multi-source 验证 (单条用户行为不直接写, ≥3 次同模式)
- 重大改动用户确认 (核心字段改动每次/每周/每月可配置)
- prompt injection 防护 (检测"忘记之前所有偏好"等模式)
- 导出/备份/删除权 (用户可一键)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id

logger = logging.getLogger(__name__)


# Prompt injection 检测模式 (V2.1 §12.8)
INJECTION_PATTERNS = [
    re.compile(r"忘记.*(之前|所有).*偏好", re.S),
    re.compile(r"forget\s+(all\s+)?previous\s+preferences", re.I),
    re.compile(r"现在你是\s*\w+", re.S),
    re.compile(r"now\s+you\s+are\s+", re.I),
    re.compile(r"system:\s*(升级|elevate|admin)", re.I),
    re.compile(r"ignore\s+(previous|all)\s+instructions", re.I),
    re.compile(r"override\s+(your|the)\s+(rules|behavior)", re.I),
]

# 核心字段 (改动需用户确认)
CORE_FIELDS = (
    "approval_threshold_money",
    "approval_threshold_irreversible",
    "trusted_models",
    "distrusted_models",
    "professional_role",
    "risk_tolerance",
)


@dataclass
class EvolvedTrait:
    """灵魂档案演化出的特征."""

    trait: str
    evidence_count: int = 1
    first_observed: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_observed: datetime = field(default_factory=lambda: datetime.now(UTC))
    sources: list[str] = field(default_factory=list)


class SoulFileRevision(BaseModel):
    """append-only 历史条目."""

    revision_id: str = Field(default_factory=lambda: new_id("memory"))
    revised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    field_path: str  # 如 "approval_threshold_money"
    old_value: Any = None
    new_value: Any = None
    reason: Literal[
        "user_explicit",
        "system_inferred",
        "evidence_threshold_met",
        "user_confirmed_inferred",
        "policy_required",
    ]
    evidence_count: int = 1
    causing_event_id: str | None = None
    requires_confirmation: bool = False
    confirmed_by_user_at: datetime | None = None


class SoulFile(BaseModel):
    """user 级灵魂档案."""

    user_id: str
    tenant_id: str = "u-sylvan"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # 风格
    audience: Literal["novice", "developer", "expert"] = "developer"
    default_language: str = "zh-CN"
    output_format_preference: Literal["concise", "detailed", "structured"] = "concise"

    # 信任与风险
    trusted_models: list[dict[str, Any]] = Field(default_factory=list)
    distrusted_models: list[str] = Field(default_factory=list)
    approval_threshold_money: float = 10.0
    approval_threshold_irreversible: Literal["always", "never", "per_action"] = "always"
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    cost_sensitivity: Literal["low", "medium", "high"] = "medium"
    speed_sensitivity: Literal["low", "medium", "high"] = "medium"
    interruption_tolerance: Literal["low", "medium", "high"] = "medium"
    interruption_frequency: Literal["full_auto", "ask_every_n", "manual_review"] = "ask_every_n"
    ask_every_n_steps: int = Field(default=5, ge=1)

    # 工具偏好
    preferred_tools: list[dict[str, Any]] = Field(default_factory=list)
    pinned_assets: list[str] = Field(default_factory=list)

    # 领域专长
    domain_expertise: list[dict[str, str]] = Field(default_factory=list)
    professional_role: str = ""

    # 时区 / 日历
    timezone: str = "Asia/Shanghai"
    working_hours: str = "09:00-19:00"

    # 通知
    notification_preference: dict[str, str] = Field(default_factory=dict)

    # 演化特征 (自动累积)
    evolved_traits: list[EvolvedTrait] = Field(default_factory=list)

    # 自由扩展槽 (V2.1 §13.6)
    extensions: dict[str, Any] = Field(default_factory=dict)

    # append-only 历史 (governance 核心)
    revision_history: list[SoulFileRevision] = Field(default_factory=list)

    def should_interrupt_at_step(self, step_index: int) -> bool:
        """按用户中断频率偏好决定当前 step 后是否打扰用户."""

        if self.interruption_frequency == "full_auto":
            return False
        if self.interruption_frequency == "manual_review":
            return True
        return step_index > 0 and step_index % self.ask_every_n_steps == 0


@dataclass
class SoulWriteResult:
    """写灵魂档案的结果."""

    accepted: bool
    revision_id: str | None = None
    rejected_reason: str = ""
    requires_confirmation: bool = False
    awaiting_confirmation_token: str | None = None


class SoulFileGovernance:
    """灵魂档案 governance 引擎 (V2.1 §12.8 + §20.4 / T44).

    职责:
    - prompt injection 检测
    - multi-source 验证 (≥3 次同模式才写)
    - 核心字段改动需用户确认
    - append-only 写入 (修改 = 加 revision, 不删原条目)
    """

    def __init__(
        self,
        *,
        evidence_threshold: int = 3,
        confirmation_required_for_core: bool = True,
        injection_patterns: list[re.Pattern[str]] | None = None,
    ) -> None:
        self.evidence_threshold = evidence_threshold
        self.confirmation_required_for_core = confirmation_required_for_core
        self.injection_patterns = injection_patterns or list(INJECTION_PATTERNS)
        # 累积证据 (key: (field, value_hash) → count)
        self._evidence_counts: dict[tuple[str, str], int] = {}
        # 待确认队列 (token → (soul, revision))
        self._pending_confirms: dict[str, tuple[SoulFile, SoulFileRevision]] = {}

    @staticmethod
    def detect_injection(text: str) -> tuple[bool, str | None]:
        """检测 prompt injection."""
        for pat in INJECTION_PATTERNS:
            m = pat.search(text)
            if m:
                return (True, pat.pattern)
        return (False, None)

    def write_field(
        self,
        soul: SoulFile,
        field_path: str,
        new_value: Any,
        *,
        reason: Literal[
            "user_explicit",
            "system_inferred",
            "evidence_threshold_met",
            "user_confirmed_inferred",
            "policy_required",
        ],
        causing_event_id: str | None = None,
        accompanying_text: str = "",
    ) -> SoulWriteResult:
        """写入字段. 走 append-only + governance 规则."""
        # 1. injection 防护 (检查是否有"系统注入"内容)
        if accompanying_text:
            is_inj, pat = self.detect_injection(accompanying_text)
            if is_inj:
                logger.warning(
                    "soul write blocked: injection pattern %s for user %s",
                    pat,
                    soul.user_id,
                )
                return SoulWriteResult(
                    accepted=False,
                    rejected_reason=f"prompt_injection_detected:{pat}",
                )

        # 2. system_inferred → 累积证据 (≥ threshold 才写)
        if reason == "system_inferred":
            value_hash = _hash_value(new_value)
            key = (field_path, value_hash)
            self._evidence_counts[key] = self._evidence_counts.get(key, 0) + 1
            count = self._evidence_counts[key]
            if count < self.evidence_threshold:
                return SoulWriteResult(
                    accepted=False,
                    rejected_reason=(
                        f"evidence_count_{count}<_threshold_{self.evidence_threshold}"
                    ),
                )
            # 升级到 evidence_threshold_met
            reason = "evidence_threshold_met"

        # 3. 核心字段改动 → 需用户确认
        if (
            self.confirmation_required_for_core
            and field_path in CORE_FIELDS
            and reason not in ("user_explicit", "user_confirmed_inferred", "policy_required")
        ):
            old_val = getattr(soul, field_path, None)
            revision = SoulFileRevision(
                field_path=field_path,
                old_value=old_val,
                new_value=new_value,
                reason=reason,
                evidence_count=self._evidence_counts.get(
                    (field_path, _hash_value(new_value)),
                    0,
                ),
                causing_event_id=causing_event_id,
                requires_confirmation=True,
            )
            token = revision.revision_id[-8:].upper()
            self._pending_confirms[token] = (soul, revision)
            return SoulWriteResult(
                accepted=False,
                requires_confirmation=True,
                awaiting_confirmation_token=token,
            )

        # 4. 通过 → append revision + apply
        old_val = getattr(soul, field_path, None)
        revision = SoulFileRevision(
            field_path=field_path,
            old_value=old_val,
            new_value=new_value,
            reason=reason,
            evidence_count=self._evidence_counts.get(
                (field_path, _hash_value(new_value)),
                1,
            ),
            causing_event_id=causing_event_id,
        )
        soul.revision_history.append(revision)
        if hasattr(soul, field_path):
            setattr(soul, field_path, new_value)
        else:
            soul.extensions[field_path] = new_value
        soul.last_updated_at = datetime.now(UTC)

        return SoulWriteResult(
            accepted=True,
            revision_id=revision.revision_id,
        )

    def confirm_pending(
        self,
        token: str,
        accept: bool = True,
    ) -> SoulWriteResult:
        """用户确认/拒绝 pending revision."""
        pending = self._pending_confirms.pop(token, None)
        if pending is None:
            return SoulWriteResult(
                accepted=False,
                rejected_reason="unknown_or_expired_token",
            )
        soul, revision = pending
        if not accept:
            return SoulWriteResult(
                accepted=False,
                rejected_reason="user_rejected",
            )
        # 接受: 重新写入 (跳过确认)
        return self.write_field(
            soul,
            revision.field_path,
            revision.new_value,
            reason="user_confirmed_inferred",
            causing_event_id=revision.causing_event_id,
        )

    def add_evolved_trait(
        self,
        soul: SoulFile,
        trait: str,
        evidence: str,
    ) -> bool:
        """累积演化特征 (低敏感, 不需用户确认)."""
        for t in soul.evolved_traits:
            if t.trait == trait:
                t.evidence_count += 1
                t.last_observed = datetime.now(UTC)
                t.sources.append(evidence)
                return False  # 已存在, 增计数
        soul.evolved_traits.append(
            EvolvedTrait(
                trait=trait,
                sources=[evidence],
            )
        )
        return True  # 新增

    def export(self, soul: SoulFile) -> dict[str, Any]:
        """导出灵魂档案 (用户可备份)."""
        return soul.model_dump()

    def get_pending_confirmations(self) -> dict[str, tuple[SoulFile, SoulFileRevision]]:
        return dict(self._pending_confirms)


def _hash_value(value: Any) -> str:
    """value 哈希 (用于证据累积 key)."""
    s = str(value)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CORE_FIELDS",
    "INJECTION_PATTERNS",
    "EvolvedTrait",
    "SoulFile",
    "SoulFileGovernance",
    "SoulFileRevision",
    "SoulWriteResult",
]
