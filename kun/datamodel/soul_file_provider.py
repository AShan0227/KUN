"""SoulFile provider — 让 router / intent 能拿到 user 灵魂档案 (V2.1 wire M3.3).

opt-in 模式 (默认 off):
- KUN_SOUL_FILE_ENABLED=1 启用
- 启用后: router / intent 用 SoulFile 字段调整决策
  • audience → system prompt 风格
  • risk_tolerance / cost_sensitivity / speed_sensitivity → strategy_score 权重
  • approval_threshold_money → plan-only 触发判据
  • interruption_tolerance → 反问触发频率

M3.3: 内存 cache + 默认 SoulFile.
M4: 加 DB 持久化 (load_or_create_soul_file / save_soul_file 走 SQLAlchemy).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import select

from kun.datamodel.soul_file import SoulFile, SoulFileGovernance

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.getenv("KUN_SOUL_FILE_ENABLED", "0") == "1"


# 内存 cache: (tenant_id, user_id) → SoulFile.
# get_soul_file (sync) 走 cache; load_or_create_soul_file / save_soul_file (async)
# 走 DB. 启动时 preload_all_soul_files 把所有用户的 SoulFile 拉进 cache.
_store: dict[tuple[str, str], SoulFile] = {}
_governance = SoulFileGovernance()


def _cache_key(user_id: str, tenant_id: str) -> tuple[str, str]:
    return (tenant_id, user_id)


def get_soul_file(user_id: str, tenant_id: str = "u-sylvan") -> SoulFile:
    """取该 user 的 SoulFile (sync, cache only).

    cache miss → 返默认对象 (不查 DB, 因为 sync). 想要真值用 await
    load_or_create_soul_file().
    """
    key = _cache_key(user_id, tenant_id)
    if key not in _store:
        _store[key] = SoulFile(user_id=user_id, tenant_id=tenant_id)
    return _store[key]


def get_governance() -> SoulFileGovernance:
    return _governance


def reset_store() -> None:
    """测试用."""
    global _store, _governance
    _store = {}
    _governance = SoulFileGovernance()


# ============================================================================
# M4 DB 持久化
# ============================================================================


def _soul_file_to_row_kwargs(soul: SoulFile) -> dict[str, Any]:
    """SoulFile pydantic → SoulFileRow 字段."""
    blob = soul.model_dump(mode="json")
    return {
        "tenant_id": soul.tenant_id,
        "user_id": soul.user_id,
        "audience": soul.audience,
        "default_language": soul.default_language,
        "risk_tolerance": soul.risk_tolerance,
        "cost_sensitivity": soul.cost_sensitivity,
        "speed_sensitivity": soul.speed_sensitivity,
        "interruption_tolerance": soul.interruption_tolerance,
        "approval_threshold_money": soul.approval_threshold_money,
        "professional_role": soul.professional_role,
        "blob": blob,
    }


def _row_to_soul_file(row: Any) -> SoulFile:
    """SoulFileRow → SoulFile pydantic. blob 优先 (含 nested), 拍平字段作 fallback."""
    blob = row.blob or {}
    if blob:
        try:
            return SoulFile.model_validate(blob)
        except Exception:
            logger.exception("soul_file blob deserialization failed, falling back to flat fields")
    return SoulFile(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        audience=row.audience,
        default_language=row.default_language,
        risk_tolerance=row.risk_tolerance,
        cost_sensitivity=row.cost_sensitivity,
        speed_sensitivity=row.speed_sensitivity,
        interruption_tolerance=row.interruption_tolerance,
        approval_threshold_money=row.approval_threshold_money,
        professional_role=row.professional_role,
    )


async def load_or_create_soul_file(user_id: str, tenant_id: str = "u-sylvan") -> SoulFile:
    """从 DB 拉, 不存在则创建默认 + insert. 同步更新 cache."""
    from kun.core.db import session_scope
    from kun.core.orm import SoulFileRow

    key = _cache_key(user_id, tenant_id)
    async with session_scope(tenant_id=tenant_id) as s:
        stmt = select(SoulFileRow).where(
            SoulFileRow.tenant_id == tenant_id, SoulFileRow.user_id == user_id
        )
        row = (await s.execute(stmt)).scalar_one_or_none()
        if row is None:
            soul = SoulFile(user_id=user_id, tenant_id=tenant_id)
            new_row = SoulFileRow(**_soul_file_to_row_kwargs(soul))
            s.add(new_row)
        else:
            soul = _row_to_soul_file(row)
    _store[key] = soul
    return soul


async def save_soul_file(soul: SoulFile) -> None:
    """upsert SoulFile 到 DB + 更新 cache."""
    from sqlalchemy.dialects.postgresql import insert

    from kun.core.db import session_scope
    from kun.core.orm import SoulFileRow

    kwargs = _soul_file_to_row_kwargs(soul)
    async with session_scope(tenant_id=soul.tenant_id) as s:
        stmt = insert(SoulFileRow).values(**kwargs)
        # ON CONFLICT (tenant_id, user_id) DO UPDATE
        update_dict = {k: v for k, v in kwargs.items() if k not in ("tenant_id", "user_id")}
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "user_id"],
            set_=update_dict,
        )
        await s.execute(stmt)
    _store[_cache_key(soul.user_id, soul.tenant_id)] = soul


async def preload_all_soul_files(tenant_id: str = "u-sylvan") -> int:
    """启动时把所有 SoulFile 拉进 cache, 返加载条数."""
    from kun.core.db import session_scope
    from kun.core.orm import SoulFileRow

    count = 0
    async with session_scope(tenant_id=tenant_id) as s:
        stmt = select(SoulFileRow).where(SoulFileRow.tenant_id == tenant_id)
        rows = (await s.execute(stmt)).scalars().all()
        for row in rows:
            soul = _row_to_soul_file(row)
            _store[_cache_key(soul.user_id, soul.tenant_id)] = soul
            count += 1
    logger.info("soul_file.preloaded", extra={"tenant_id": tenant_id, "count": count})
    return count


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
    "load_or_create_soul_file",
    "preload_all_soul_files",
    "reset_store",
    "save_soul_file",
    "soul_file_to_router_overrides",
    "soul_file_to_signal_user_dict",
]
