"""SoulFile provider — 让 router / intent 能拿到 user 灵魂档案 (V2.1 wire M3.3).

opt-in 模式 (默认 off):
- KUN_SOUL_FILE_ENABLED=1 启用
- 启用后: router / intent 用 SoulFile 字段调整决策
  • audience → system prompt 风格
  • risk_tolerance / cost_sensitivity / speed_sensitivity → strategy_score 权重
  • approval_threshold_money → plan-only 触发判据
  • interruption_tolerance → 反问触发频率

M3.3 stub: 内存 store + 默认 SoulFile (M4 接 SQLAlchemy + alembic).
"""

from __future__ import annotations

import logging
import os

from kun.datamodel.soul_file import SoulFile, SoulFileGovernance

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.getenv("KUN_SOUL_FILE_ENABLED", "0") == "1"


# 内存 store: user_id → SoulFile (M3.3 简版, M4 接 DB)
_store: dict[str, SoulFile] = {}
_governance = SoulFileGovernance()


def get_soul_file(user_id: str, tenant_id: str = "u-sylvan") -> SoulFile:
    """取该 user 的 SoulFile, 不存在则按默认值创建."""
    if user_id not in _store:
        _store[user_id] = SoulFile(user_id=user_id, tenant_id=tenant_id)
    return _store[user_id]


def get_governance() -> SoulFileGovernance:
    return _governance


def reset_store() -> None:
    """测试用."""
    global _store, _governance
    _store = {}
    _governance = SoulFileGovernance()


def soul_file_to_router_overrides(soul: SoulFile) -> dict[str, object]:
    """把 SoulFile 字段转成 router 决策 overrides.

    router/intent 调用 get_soul_file() → 用这里的产出影响决策.
    """
    return {
        "audience": soul.audience,
        "language": soul.default_language,
        "trusted_models": [m["model_id"] for m in soul.trusted_models if "model_id" in m],
        "distrusted_models": list(soul.distrusted_models),
        "approval_threshold_money": soul.approval_threshold_money,
        "approval_threshold_irreversible": soul.approval_threshold_irreversible,
        "risk_tolerance": soul.risk_tolerance,
        "cost_sensitivity": soul.cost_sensitivity,
        "speed_sensitivity": soul.speed_sensitivity,
        "interruption_tolerance": soul.interruption_tolerance,
        "professional_role": soul.professional_role,
        "evolved_traits": [
            {"trait": t.trait, "evidence_count": t.evidence_count} for t in soul.evolved_traits
        ],
    }


def soul_file_to_signal_user_dict(soul: SoulFile) -> dict[str, object]:
    """把 SoulFile 转成 SignalBundle.user dict (V2.1 §17.2)."""
    return {
        "user_id": soul.user_id,
        "audience": soul.audience,
        "trusted_models": [
            (m.get("model_id"), m.get("trust_level", 0.5)) for m in soul.trusted_models
        ],
        "distrusted_models": list(soul.distrusted_models),
        "approval_threshold_money": soul.approval_threshold_money,
        "approval_threshold_irreversible": soul.approval_threshold_irreversible,
        "risk_tolerance": soul.risk_tolerance,
        "cost_sensitivity": soul.cost_sensitivity,
        "speed_sensitivity": soul.speed_sensitivity,
        "interruption_tolerance": soul.interruption_tolerance,
        "user_role": soul.professional_role or "unknown",
    }


__all__ = [
    "get_governance",
    "get_soul_file",
    "is_enabled",
    "reset_store",
    "soul_file_to_router_overrides",
    "soul_file_to_signal_user_dict",
]
