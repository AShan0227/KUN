"""自指限制 (ADR-024 §约束 1) — 唯一 source of truth.

集中 5 个监督角色前缀 + `is_self_referential(target_module)` 检查逻辑.
之前散在 strategist/service.py 和 supervisor/escalation.py 各一份, L3.5
统一到这里, 让 Gate / Strategist / Supervisor / 未来 LLM 角色 都引用同一套.

定义:
  target_module 命中以下 5 个监督角色前缀任一种命名形式即 self-referential:
    strategist / supervisor / gate / director / external_supervisor

  4 种命名形式:
    1. 裸名 ('strategist')
    2. 点路径 ('strategist.service')
    3. kun.agents.<role> ('kun.agents.strategist')
    4. kun/agents/<role> ('kun/agents/strategist/service')

工程化护栏:
  - 自指 capability 永远不 auto-promote (Gate enforce)
  - Strategist 自指候选强制 target_level=0 (设计层) — 改自己属于设计决策
  - Escalation 自指追加 L4 human path
  - Capability writeback metadata 标 promotion_block_self_referential=True
"""

from __future__ import annotations

SELF_REFERENTIAL_PREFIXES: tuple[str, ...] = (
    "strategist",
    "supervisor",
    "gate",
    "director",
    "external_supervisor",
)

# Real module roots whose code IS the RSI judge / supervision / guard machinery.
# Editing anything under these is modifying the mechanism that judges the change
# itself — self-referential regardless of role-prefix naming. The prefix-only
# check above missed these real paths (audit F033): the External Supervisor lives
# at kun/external_supervisor (NOT kun/agents/external_supervisor), and the
# governance/watchtower guard code (incl. this very file) carries no role prefix.
SELF_REFERENTIAL_MODULE_ROOTS: tuple[str, ...] = (
    "kun/external_supervisor",
    "kun/governance",
    "kun/watchtower",
)


def is_self_referential(target_module: str | None) -> bool:
    """target_module 命中 5 个监督角色前缀、或 RSI 判定/护栏真实模块根 → True.

    None / 空字符串 → False.
    """
    if not target_module:
        return False
    lowered = target_module.lower()
    for prefix in SELF_REFERENTIAL_PREFIXES:
        if (
            lowered == prefix
            or lowered.startswith(f"{prefix}.")
            or lowered.startswith(f"{prefix}/")
            or lowered.startswith(f"kun/agents/{prefix}")
            or lowered.startswith(f"kun.agents.{prefix}")
        ):
            return True
    for root in SELF_REFERENTIAL_MODULE_ROOTS:
        dotted = root.replace("/", ".")
        if (
            lowered in (root, dotted)
            or lowered.startswith(f"{root}/")
            or lowered.startswith(f"{dotted}.")
        ):
            return True
    return False


__all__ = [
    "SELF_REFERENTIAL_MODULE_ROOTS",
    "SELF_REFERENTIAL_PREFIXES",
    "is_self_referential",
]
