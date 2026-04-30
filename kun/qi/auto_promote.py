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
    blocked_no_evidence = 0
    replay_evidence: dict[str, Any] | None = None

    try:
        if os.getenv("KUN_PROTOCOL_REPLAY_EVALUATOR_ENABLED", "1") == "1":
            try:
                from kun.qi.protocol_replay import ProtocolReplayEvaluator

                replay_evidence = await ProtocolReplayEvaluator().evaluate_missing_evidence(
                    registry,
                    tenant_id,
                )
            except Exception as e:
                log.warning("auto_promote.replay_evidence_failed", error=str(e))
        all_protocols = await registry.list_all(tenant_id)
    except Exception as e:
        log.warning("auto_promote.list_failed", error=str(e))
        return {"error": str(e)}

    for proto in all_protocols:
        if proto.status not in _PROMOTE_RULES:
            continue
        rule = _PROMOTE_RULES[proto.status]
        meta = proto.metadata or {}
        evidence = _promotion_evidence(meta)
        if evidence is None:
            blocked_no_evidence += 1
            kept += 1
            log.info(
                "auto_promote.blocked_no_evidence",
                protocol_id=proto.protocol_id,
                version=proto.version,
                status=proto.status,
            )
            continue

        score = evidence["win_rate"]
        runs = int(evidence["runs"])
        guardrail_pass = bool(evidence.get("guardrail_pass", True))
        if not guardrail_pass:
            skipped += 1
            log.info(
                "auto_promote.blocked_guardrail",
                protocol_id=proto.protocol_id,
                version=proto.version,
                status=proto.status,
                source=evidence.get("source", ""),
            )
            continue

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
                    runs=runs,
                    evidence_source=evidence.get("source", ""),
                )
            except Exception as e:
                log.debug(
                    "auto_promote.promote_failed", protocol_id=proto.protocol_id, error=str(e)
                )
                skipped += 1
        else:
            kept += 1

    return {
        "promoted": promoted,
        "kept": kept,
        "skipped": skipped,
        "blocked_no_evidence": blocked_no_evidence,
        "replay_evidence": replay_evidence,
    }


def _promotion_evidence(meta: dict[str, Any]) -> dict[str, Any] | None:
    """读取真实晋升证据。

    以前这里把 darwin_best_score 当 win_rate 用，容易把“LLM 写得像样”
    误判成“真实任务效果好”。V4 改成必须有 replay/canary/benchmark
    这类外部证据，Darwin 分数只能证明“值得进入实验”，不能直接晋升。
    """
    raw = meta.get("promotion_evidence")
    if isinstance(raw, dict):
        runs = raw.get("runs")
        if runs is None:
            runs = raw.get("sample_size", 0)
        win_rate = raw.get("win_rate")
        if win_rate is None:
            win_rate = raw.get("success_rate", 0.0)
        try:
            return {
                "runs": max(0, int(runs)),
                "win_rate": max(0.0, min(1.0, float(win_rate))),
                "guardrail_pass": bool(raw.get("guardrail_pass", True)),
                "source": str(raw.get("source") or "promotion_evidence"),
            }
        except (TypeError, ValueError):
            return None

    # Backward-compatible explicit fields, but still require the evidence prefix.
    if "evidence_runs" in meta or "evidence_win_rate" in meta:
        try:
            return {
                "runs": max(0, int(meta.get("evidence_runs", 0))),
                "win_rate": max(0.0, min(1.0, float(meta.get("evidence_win_rate", 0.0)))),
                "guardrail_pass": bool(meta.get("evidence_guardrail_pass", True)),
                "source": str(meta.get("evidence_source") or "metadata"),
            }
        except (TypeError, ValueError):
            return None

    return None


__all__ = ["auto_promote_protocols"]
