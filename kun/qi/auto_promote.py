"""V2.3+ 协议自动 promote (cron).

experimental 跑过 N 次 + win_rate >= threshold → 自动 promote shadow → canary → stable.
KUN_PROTOCOL_AUTO_PROMOTE_ENABLED=1 (default ON 内测).

跟启 cron 一起跑 (hourly).
"""

from __future__ import annotations

import os
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.qi.auto_promote")

# 默认阈值: experimental → shadow 需 5 次 + win_rate 0.5
# shadow → canary 需 20 次 + win_rate 0.65
# canary → stable 需 50 次 + win_rate 0.75
_PROMOTE_RULES: dict[str, dict[str, Any]] = {
    "experimental": {"min_runs": 5, "min_win_rate": 0.5, "next": "shadow"},
    "shadow": {"min_runs": 20, "min_win_rate": 0.65, "next": "canary"},
    "canary": {"min_runs": 50, "min_win_rate": 0.75, "next": "stable"},
}


async def auto_promote_protocols(app: Any, tenant_id: str) -> dict[str, Any]:
    """跑一次自动 promote 检查. 返 {promoted: N, kept: N, skipped: N}."""
    if os.getenv("KUN_PROTOCOL_AUTO_PROMOTE_ENABLED", "1") != "1":
        return {"skipped": True, "reason": "KUN_PROTOCOL_AUTO_PROMOTE_ENABLED=0"}

    registry = getattr(app.state, "protocol_registry", None)
    if registry is None:
        return {"skipped": True, "reason": "no protocol_registry"}

    promoted = 0
    kept = 0
    skipped = 0

    try:
        all_protocols = await registry.list_all(tenant_id)
    except Exception as e:
        log.warning("auto_promote.list_failed", error=str(e))
        return {"error": str(e)}

    for proto in all_protocols:
        if proto.status not in _PROMOTE_RULES:
            continue
        rule = _PROMOTE_RULES[proto.status]
        # 简化: 没真 task_runs 计数, 用 metadata.darwin_best_score 当 win_rate 代理
        # V2.4 加真 task outcome counter
        meta = proto.metadata or {}
        score = float(meta.get("darwin_best_score", 0.0))
        runs = int(meta.get("runs", 1))  # 默认 1 次

        if runs >= int(rule["min_runs"]) and score >= float(rule["min_win_rate"]):
            try:
                await registry.promote(
                    tenant_id, proto.protocol_id, proto.version, str(rule["next"])
                )
                promoted += 1
                log.info(
                    "auto_promote.promoted",
                    protocol_id=proto.protocol_id,
                    version=proto.version,
                    from_status=proto.status,
                    to_status=rule["next"],
                    score=score,
                )
            except Exception as e:
                log.debug("auto_promote.promote_failed", protocol_id=proto.protocol_id, error=str(e))
                skipped += 1
        else:
            kept += 1

    return {"promoted": promoted, "kept": kept, "skipped": skipped}


__all__ = ["auto_promote_protocols"]
