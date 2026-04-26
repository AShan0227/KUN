"""Honesty Tier — 诚实性自检 4 档力度 (V2.1 §17.4 / T33).

按 risk_level 动态匹配, 低风险静默, 高风险强制 plan-only + multi-judge.

| risk_level | 自检策略 | 成本 | 用户感知 |
|-----------|---------|------|---------|
| low | 静默 confidence (不显示标记) | 1.00x | 流畅 |
| medium | confidence + source 分级 (4 档) | 1.05x | 略多元数据 |
| high | + 可验证产物必须真测试/试跑/真 URL | 1.30x | 明显 |
| critical | + multi_judge 投票 + plan-only + dev/prod 隔离 | 3-5x | 强制审批 |

避免反噬流畅: 低风险任务静默处理, 不堆"我有 60% 把握"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


HonestyLevel = Literal["silent", "metadata", "verified", "guarded"]
RiskLevel = Literal["low", "medium", "high", "critical"]
SourceCategory = Literal["fact", "verified_resource", "public_knowledge", "speculation"]


# risk_level → honesty_level 默认映射
RISK_TO_HONESTY: dict[RiskLevel, HonestyLevel] = {
    "low": "silent",
    "medium": "metadata",
    "high": "verified",
    "critical": "guarded",
}


@dataclass
class SourceClaim:
    """单个事实源声明."""

    claim_text: str
    source_category: SourceCategory
    source_url: str = ""
    confidence: float = 1.0  # 0-1


@dataclass
class HonestyAnnotation:
    """诚实性自检产出 (随回答带回)."""

    level: HonestyLevel
    overall_confidence: float = 1.0  # 0-1
    sources: list[SourceClaim] = field(default_factory=list)
    not_deeply_understood: list[str] = field(default_factory=list)
    requires_verification: list[str] = field(default_factory=list)
    multi_judge_verdict: dict[str, Any] | None = None
    requires_plan_only: bool = False
    requires_dev_prod_isolation: bool = False

    def to_user_visible(self) -> dict[str, Any] | None:
        """根据 level 决定是否给用户看 (低 risk 静默)."""
        if self.level == "silent":
            return None  # 不展示, 内部记录即可
        out: dict[str, Any] = {
            "level": self.level,
            "confidence": round(self.overall_confidence, 2),
        }
        if self.level in ("metadata", "verified", "guarded"):
            out["sources"] = [
                {
                    "claim": s.claim_text,
                    "category": s.source_category,
                    "url": s.source_url,
                }
                for s in self.sources
            ]
        if self.not_deeply_understood:
            out["not_deeply_understood"] = self.not_deeply_understood
        if self.level in ("verified", "guarded"):
            out["requires_verification"] = self.requires_verification
        if self.level == "guarded":
            if self.multi_judge_verdict:
                out["multi_judge"] = self.multi_judge_verdict
            out["plan_only"] = self.requires_plan_only
            out["dev_prod_isolated"] = self.requires_dev_prod_isolation
        return out


class HonestyTierMatcher:
    """根据 risk_level 决定自检力度 (V2.1 §17.4)."""

    def __init__(
        self,
        *,
        risk_to_honesty: dict[RiskLevel, HonestyLevel] | None = None,
    ) -> None:
        self.mapping = risk_to_honesty or RISK_TO_HONESTY

    def determine_level(
        self,
        risk_level: RiskLevel,
        user_override: HonestyLevel | None = None,
    ) -> HonestyLevel:
        """决定自检力度. 用户偏好可覆盖."""
        if user_override is not None:
            return user_override
        return self.mapping.get(risk_level, "metadata")

    def annotate(
        self,
        *,
        risk_level: RiskLevel,
        answer_text: str,
        sources: list[SourceClaim] | None = None,
        not_deeply_understood: list[str] | None = None,
        verification_results: list[dict[str, Any]] | None = None,
        multi_judge_verdict: dict[str, Any] | None = None,
        user_override: HonestyLevel | None = None,
    ) -> HonestyAnnotation:
        """生成 HonestyAnnotation, 自动按 level 填字段."""
        level = self.determine_level(risk_level, user_override)
        sources = sources or []

        # confidence 计算: 按 sources 平均 + 多 judge 一致性加权
        if sources:
            avg_conf = sum(s.confidence for s in sources) / len(sources)
        else:
            avg_conf = 0.7  # 没明示 source → 中等信心

        if multi_judge_verdict and "consensus" in multi_judge_verdict:
            consensus = float(multi_judge_verdict["consensus"])
            avg_conf = (avg_conf + consensus) / 2

        ann = HonestyAnnotation(
            level=level,
            overall_confidence=avg_conf,
            sources=sources,
            not_deeply_understood=not_deeply_understood or [],
        )

        # high / guarded 加 verification 要求
        if level in ("verified", "guarded"):
            ann.requires_verification = [
                v.get("kind", "unknown")
                for v in (verification_results or [])
                if not v.get("passed", False)
            ]

        # critical / guarded 强制 multi_judge + plan-only + dev/prod
        if level == "guarded":
            ann.multi_judge_verdict = multi_judge_verdict
            ann.requires_plan_only = True
            ann.requires_dev_prod_isolation = True
            # 没 verdict → 标 confidence 低
            if not multi_judge_verdict:
                ann.overall_confidence = min(ann.overall_confidence, 0.5)

        return ann

    def estimated_cost_multiplier(self, level: HonestyLevel) -> float:
        """该 level 的成本相对 base 倍数 (V2.1 §17.4 表格)."""
        return {
            "silent": 1.00,
            "metadata": 1.05,
            "verified": 1.30,
            "guarded": 4.0,  # 3-5x 取中
        }[level]


__all__ = [
    "RISK_TO_HONESTY",
    "HonestyAnnotation",
    "HonestyLevel",
    "HonestyTierMatcher",
    "RiskLevel",
    "SourceCategory",
    "SourceClaim",
]
