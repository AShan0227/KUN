"""V2.4: 多窗口 + 自动选最便宜窗口 (启 V3.5).

支持配置 N 个时间窗口 (e.g. 凌晨 + 午休 + 晚饭). 自动选当前最划算的:
- 优先 LLM provider 价格低的时段 (现在简化: 都按当前小时直接判)
- KUN_QI_MULTI_WINDOWS_ENABLED=1 (default ON 内测)
- 配置: KUN_QI_WINDOWS_JSON='[{"start":1,"end":5},{"start":12,"end":13}]'
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from kun.qi.window import QiWindowConfig


def get_active_windows() -> list[QiWindowConfig]:
    """返当前配置的所有窗口列表 (单个或多个)."""
    if os.getenv("KUN_QI_MULTI_WINDOWS_ENABLED", "1") != "1":
        return [QiWindowConfig(enabled=True)]

    raw = os.getenv("KUN_QI_WINDOWS_JSON")
    if raw:
        try:
            arr = json.loads(raw)
            return [
                QiWindowConfig(
                    enabled=True,
                    start_hour=int(w.get("start", 2)),
                    end_hour=int(w.get("end", 5)),
                    weekdays=tuple(int(d) for d in w.get("weekdays", range(7))),
                )
                for w in arr
            ]
        except Exception:
            pass

    # default: 3 个窗口
    return [
        QiWindowConfig(enabled=True, start_hour=1, end_hour=5),  # 深夜
        QiWindowConfig(enabled=True, start_hour=12, end_hour=13),  # 午休
        QiWindowConfig(enabled=True, start_hour=22, end_hour=23),  # 睡前
    ]


def is_any_window_active(when: datetime | None = None) -> bool:
    """任一窗口活跃 → True."""
    moment = when or datetime.now(UTC)
    return any(w.covers(moment) for w in get_active_windows())


__all__ = ["get_active_windows", "is_any_window_active"]
