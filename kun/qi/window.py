"""启 V3 时间窗口守门 (V2.3 §4.1).

启默认日常关闭. 只在 qi_window 内 (e.g. 凌晨 2-5 点) 自动启动.
所有启的高成本入口 (Darwin Gödel / AI Scientist / 大量 ensemble) 必须
调 require_qi_active() 守门.

用户可以手动 override:
    KUN_QI_FORCE_ACTIVE=1  # 临时强制启用 (即使在窗口外)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class QiWindowError(RuntimeError):
    """启窗口外尝试使用启功能."""


@dataclass(frozen=True)
class QiWindowConfig:
    """SoulFile.qi_window 的结构化形式."""

    enabled: bool = False
    start_hour: int = 2
    end_hour: int = 5
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 0=Mon
    timezone: str = "UTC"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> QiWindowConfig:
        if raw is None:
            return cls()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            start_hour=int(raw.get("start_hour", 2)),
            end_hour=int(raw.get("end_hour", 5)),
            weekdays=tuple(int(d) for d in raw.get("weekdays", range(7))),
            timezone=str(raw.get("timezone", "UTC")),
        )

    def covers(self, when: datetime) -> bool:
        """when (UTC) 是否在窗口内."""
        if not self.enabled:
            return False
        if when.weekday() not in self.weekdays:
            return False
        hour = when.hour
        # 跨午夜窗口 (e.g. 22-2): start > end
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour


def is_qi_window_active(
    config: QiWindowConfig | None = None,
    *,
    when: datetime | None = None,
) -> bool:
    """判断当前时刻是否在启窗口内.

    优先级:
      1. KUN_QI_FORCE_ACTIVE=1 → 强制 True (用户手动启)
      2. KUN_QI_FORCE_DISABLE=1 → 强制 False (紧急关)
      3. config.enabled + 时间窗口判断
    """
    if os.getenv("KUN_QI_FORCE_DISABLE") == "1":
        return False
    if os.getenv("KUN_QI_FORCE_ACTIVE") == "1":
        return True
    if config is None:
        return False
    moment = when or datetime.now(UTC)
    return config.covers(moment)


def require_qi_active(
    config: QiWindowConfig | None = None,
    *,
    when: datetime | None = None,
) -> None:
    """守门函数 — 启功能入口必调. 不在窗口内 → raise QiWindowError.

    用法:
        from kun.qi import require_qi_active
        async def run_darwin_godel(...):
            require_qi_active(get_qi_window_config())
            # ... 高成本探索逻辑
    """
    if not is_qi_window_active(config, when=when):
        raise QiWindowError(
            "启 (Qi) 当前不可用 (窗口外或 disabled). "
            "set KUN_QI_FORCE_ACTIVE=1 临时启用, 或检查 SoulFile.qi_window."
        )
