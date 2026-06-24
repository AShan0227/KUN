"""自创 RSI 请求生成 — cluster + RCDH diagnostic 综合 → rich request (L5.4).

L5.1 已产 cluster strategy_search_request (anomaly_cluster triggered);
L5.4 增强: 若同时跑 RCDH 拿到 DiagnosticRecord, 合并诊断信息到 request,
让 Strategist 接到时已经知道:

  - root_cause_level (0/1/2/3) → 指引 Strategist 选 candidate 的 target_level
  - recommended_action (redesign/activate/module_rsi/code_fix) → 指引 Explorer mode
  - scope_modules (≤5) → 指引 candidate 应限定在哪些模块上

工程化合并, 不调 LLM. caller (e.g. SupervisorPool 内 fan-out 后) 在已有
cluster 基础上调 enrich_with_diagnostic 增强请求.
"""

from __future__ import annotations

from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.self_created_request")


_ACTION_TO_LEVEL_HINT: dict[str, int] = {
    "redesign": 0,
    "activate": 1,
    "module_rsi": 2,
    "code_fix": 3,
}
"""recommended_action → 推荐 Strategist candidate.target_level."""


_ACTION_TO_EXPLORER_HINT: dict[str, str] = {
    "redesign": "aggressive",  # 设计层改, Aggressive 大动
    "activate": "conservative",  # 激活层 = 配置改, Conservative 优先
    "module_rsi": "conservative",  # 模块层 = 渐进改
    "code_fix": "performance",  # 代码 fix 通常是窄改, shadow 即可
}
"""recommended_action → 推荐 Strategist 优先 Explorer mode."""


def enrich_with_diagnostic(
    request: dict[str, Any],
    diagnostic: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 DiagnosticRecord 信息合并到 strategy_search_request.

    diagnostic 字段 (来自 RCDH run_diagnostic):
      - diagnostic_id
      - root_cause_level (0/1/2/3 or None)
      - recommended_action (redesign/activate/module_rsi/code_fix or None)
      - scope_modules (list[str], ≤5)
      - level_X_check (4 个 LevelCheckResult)

    输出: 新 request dict (原 request 不被修改), 含 rcdh_* 字段.
    """
    enriched = dict(request)
    if diagnostic is None:
        return enriched

    enriched["diagnostic_id"] = diagnostic.get("diagnostic_id")
    root_level = diagnostic.get("root_cause_level")
    enriched["rcdh_root_cause_level"] = root_level
    enriched["rcdh_recommended_action"] = diagnostic.get("recommended_action")
    enriched["rcdh_scope_modules"] = list(diagnostic.get("scope_modules") or [])

    # 给 Strategist 的提示
    action = diagnostic.get("recommended_action")
    if action:
        enriched["target_level_hint"] = _ACTION_TO_LEVEL_HINT.get(action)
        enriched["explorer_mode_hint"] = _ACTION_TO_EXPLORER_HINT.get(action)
    elif root_level is not None:
        enriched["target_level_hint"] = root_level

    # 把 RCDH evidence 合并到 evidence 列表里 (保留 source)
    rcdh_evidence: list[dict[str, Any]] = []
    for level_key in ("level_0_check", "level_1_check", "level_2_check", "level_3_check"):
        check = diagnostic.get(level_key)
        if not check:
            continue
        if not isinstance(check, dict):
            continue
        if not check.get("is_root_cause"):
            continue
        for ev in check.get("evidence") or []:
            rcdh_evidence.append(
                {
                    **ev,
                    "_from_rcdh_level": level_key,
                    "_from_diagnostic_id": diagnostic.get("diagnostic_id"),
                }
            )
    if rcdh_evidence:
        existing = list(enriched.get("evidence") or [])
        enriched["evidence"] = existing + rcdh_evidence

    log.info(
        "self_created_request.enriched",
        request_id=enriched.get("request_id"),
        diagnostic_id=diagnostic.get("diagnostic_id"),
        target_level_hint=enriched.get("target_level_hint"),
        explorer_mode_hint=enriched.get("explorer_mode_hint"),
    )
    return enriched


def build_self_created_request(
    cluster_payload: dict[str, Any],
    diagnostic: dict[str, Any] | None,
) -> dict[str, Any]:
    """组合 cluster + diagnostic → 完整自创 RSI 请求.

    cluster_payload: cluster_to_search_request() 的输出
    diagnostic: 来自 RCDH (可选)
    """
    if diagnostic is None:
        return dict(cluster_payload)
    return enrich_with_diagnostic(cluster_payload, diagnostic)


__all__ = [
    "build_self_created_request",
    "enrich_with_diagnostic",
]
