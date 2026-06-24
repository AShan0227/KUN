"""Explorer Pool 配置化 (L4.1, ADR-024 §Explorer Pool).

之前 Explorer Pool 三模式 (Conservative / Aggressive / Performance) 是
StrategistService 的硬编码. L4 让 Pool 成员可配置:

  - 通过 ExplorerPoolConfig 控制哪些 mode 启用
  - 默认仍是 3 模式; 但可缩到 1 (仅 Conservative cold-start) 或扩到 N
  - 候选生成器仍生成全 3 模式, Pool config 在 Service 端过滤
  - 配置可来自 settings (env var) 或显式构造 (测试)

设计原则:
  1. 生成器不感知 config — 仍输出"已知最佳 3 模式"; 过滤是 Service 责任
  2. unknown mode 不在默认 enabled 集合 — 显式启用才生效
  3. backward direction 走自己的路径, 不受 Explorer Pool config 影响
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

ExplorerMode = Literal[
    "conservative",
    "aggressive",
    "performance",
    "backward",  # forward/backward 决策走自己路径; 但 mode 名称统一注册
    "experimental",  # L4+ 未来 LLM-generated 候选预留
]

_DEFAULT_FORWARD_MODES: tuple[str, ...] = (
    "conservative",
    "aggressive",
    "performance",
)
_ALL_KNOWN_MODES: frozenset[str] = frozenset(
    ["conservative", "aggressive", "performance", "backward", "experimental"]
)


@dataclass(frozen=True)
class ExplorerPoolConfig:
    """Explorer Pool 配置 — 决定哪些 mode 在 forward 路径启用.

    backward direction 不在此控制 — 它走 select_repair_direction 决策路径.
    """

    enabled_forward_modes: frozenset[str] = field(
        default_factory=lambda: frozenset(_DEFAULT_FORWARD_MODES)
    )

    def is_enabled(self, mode: str) -> bool:
        """该 mode 是否在 Pool 中启用 (仅作用于 forward 候选)."""
        return mode in self.enabled_forward_modes

    def filter_candidates(self, candidates: list[Any]) -> list[Any]:
        """从 forward 候选列表过滤出启用的 mode.

        backward 候选 (explorer_mode='backward') 不过滤 — 它走自己路径.
        """
        return [
            c
            for c in candidates
            if getattr(c, "explorer_mode", "conservative") == "backward"
            or self.is_enabled(getattr(c, "explorer_mode", "conservative"))
        ]


def _parse_modes_env(raw: str) -> frozenset[str]:
    """parse 'conservative,aggressive' 风格 env var."""
    items = [m.strip().lower() for m in raw.split(",") if m.strip()]
    valid = {m for m in items if m in _ALL_KNOWN_MODES}
    return frozenset(valid)


def load_explorer_pool_config(
    env_var: str = "KUN_STRATEGIST_EXPLORER_MODES",
) -> ExplorerPoolConfig:
    """从 env var 读取配置; 缺失或非法 → fallback 默认 3 模式.

    示例:
      KUN_STRATEGIST_EXPLORER_MODES=conservative,aggressive
      → 仅启用 2 模式
      KUN_STRATEGIST_EXPLORER_MODES=conservative
      → 仅 cold-start 模式 (适合资源极紧 tenant)
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return ExplorerPoolConfig()
    modes = _parse_modes_env(raw)
    if not modes:
        # 全部非法 → 回默认
        return ExplorerPoolConfig()
    return ExplorerPoolConfig(enabled_forward_modes=modes)


__all__ = [
    "ExplorerMode",
    "ExplorerPoolConfig",
    "load_explorer_pool_config",
]
