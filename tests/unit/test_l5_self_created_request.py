"""L5.4 — Self-Created RSI Request (cluster + RCDH 综合) 单测."""

from __future__ import annotations

from typing import Any

from kun.agents.supervisor.self_created_request import (
    build_self_created_request,
    enrich_with_diagnostic,
)


def _cluster_payload(*, target_module: str = "llm.router") -> dict[str, Any]:
    """Mock 一个 cluster_to_search_request 输出."""
    return {
        "request_id": "ss-cluster-1",
        "tenant_id": "u-test",
        "triggered_by": "anomaly_cluster",
        "target_module": target_module,
        "anomaly_kind": "module_systemic",
        "evidence": [{"type": "x"}],
        "priority": "high",
        "severity": "strong",
        "created_at": "2026-05-27T00:00:00+00:00",
        "cluster_member_request_ids": ["r1", "r2"],
    }


def _diagnostic(
    *,
    root_cause_level: int | None = 1,
    recommended_action: str | None = "activate",
    scope_modules: list[str] | None = None,
    level_1_hits: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "diagnostic_id": "dx-1",
        "root_cause_level": root_cause_level,
        "recommended_action": recommended_action,
        "scope_modules": scope_modules or ["llm.router"],
        "level_0_check": {"is_root_cause": False, "evidence": []},
        "level_1_check": {
            "is_root_cause": level_1_hits is not None,
            "evidence": level_1_hits or [],
        },
        "level_2_check": {"is_root_cause": False, "evidence": []},
        "level_3_check": {"is_root_cause": False, "evidence": []},
    }


# ---- enrich_with_diagnostic ----


def test_enrich_with_none_diagnostic_returns_copy() -> None:
    payload = _cluster_payload()
    enriched = enrich_with_diagnostic(payload, None)
    assert enriched == payload
    # 是 copy 不是同 dict
    assert enriched is not payload


def test_enrich_adds_diagnostic_id_and_rcdh_fields() -> None:
    payload = _cluster_payload()
    diagnostic = _diagnostic()
    enriched = enrich_with_diagnostic(payload, diagnostic)
    assert enriched["diagnostic_id"] == "dx-1"
    assert enriched["rcdh_root_cause_level"] == 1
    assert enriched["rcdh_recommended_action"] == "activate"
    assert enriched["rcdh_scope_modules"] == ["llm.router"]


def test_enrich_maps_action_to_target_level_hint() -> None:
    payload = _cluster_payload()
    # activate → level 1
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="activate"))
    assert e["target_level_hint"] == 1
    # code_fix → level 3
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="code_fix"))
    assert e["target_level_hint"] == 3
    # redesign → level 0
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="redesign"))
    assert e["target_level_hint"] == 0


def test_enrich_maps_action_to_explorer_mode_hint() -> None:
    payload = _cluster_payload()
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="redesign"))
    assert e["explorer_mode_hint"] == "aggressive"
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="activate"))
    assert e["explorer_mode_hint"] == "conservative"
    e = enrich_with_diagnostic(payload, _diagnostic(recommended_action="code_fix"))
    assert e["explorer_mode_hint"] == "performance"


def test_enrich_falls_back_to_root_level_when_no_action() -> None:
    """recommended_action 缺时, root_cause_level 用作 target_level_hint."""
    payload = _cluster_payload()
    diagnostic = _diagnostic(recommended_action=None, root_cause_level=2)
    enriched = enrich_with_diagnostic(payload, diagnostic)
    assert enriched["target_level_hint"] == 2
    # 没 action → 没 explorer_mode_hint
    assert "explorer_mode_hint" not in enriched


def test_enrich_appends_rcdh_evidence_with_source_tag() -> None:
    payload = _cluster_payload()
    diagnostic = _diagnostic(
        level_1_hits=[{"type": "keyword", "keyword": "feature flag"}]
    )
    enriched = enrich_with_diagnostic(payload, diagnostic)
    # 原 evidence 保留 + RCDH evidence 追加
    assert len(enriched["evidence"]) == 2
    rcdh_ev = next(
        ev for ev in enriched["evidence"] if ev.get("_from_rcdh_level")
    )
    assert rcdh_ev["_from_rcdh_level"] == "level_1_check"
    assert rcdh_ev["_from_diagnostic_id"] == "dx-1"
    assert rcdh_ev["keyword"] == "feature flag"


def test_enrich_does_not_append_non_root_level_evidence() -> None:
    """L0/L2/L3 evidence is_root_cause=False → 不追加."""
    payload = _cluster_payload()
    diagnostic = {
        "diagnostic_id": "dx-1",
        "root_cause_level": 1,
        "recommended_action": "activate",
        "scope_modules": [],
        "level_0_check": {"is_root_cause": False, "evidence": [{"type": "ignored"}]},
        "level_1_check": {"is_root_cause": True, "evidence": [{"type": "real"}]},
    }
    enriched = enrich_with_diagnostic(payload, diagnostic)
    types = {ev.get("type") for ev in enriched["evidence"]}
    assert "real" in types
    assert "ignored" not in types


def test_enrich_does_not_mutate_input() -> None:
    payload = _cluster_payload()
    original_evidence = list(payload["evidence"])
    diagnostic = _diagnostic(level_1_hits=[{"type": "x"}])
    enrich_with_diagnostic(payload, diagnostic)
    # 原 payload 不变
    assert payload["evidence"] == original_evidence
    assert "diagnostic_id" not in payload


# ---- build_self_created_request ----


def test_build_with_no_diagnostic_returns_cluster_copy() -> None:
    cluster = _cluster_payload()
    out = build_self_created_request(cluster, None)
    assert out == cluster
    assert out is not cluster


def test_build_combines_cluster_and_diagnostic() -> None:
    cluster = _cluster_payload(target_module="executor.coding")
    diagnostic = _diagnostic(
        recommended_action="module_rsi",
        scope_modules=["executor.coding", "executor.refactor"],
        level_1_hits=[{"type": "x"}],
    )
    out = build_self_created_request(cluster, diagnostic)
    # 保留 cluster 字段
    assert out["target_module"] == "executor.coding"
    assert out["anomaly_kind"] == "module_systemic"
    # 加入 RCDH 字段
    assert out["target_level_hint"] == 2  # module_rsi
    assert out["explorer_mode_hint"] == "conservative"
    assert "executor.refactor" in out["rcdh_scope_modules"]


def test_build_preserves_cluster_member_ids() -> None:
    cluster = _cluster_payload()
    diagnostic = _diagnostic()
    out = build_self_created_request(cluster, diagnostic)
    assert out["cluster_member_request_ids"] == ["r1", "r2"]
