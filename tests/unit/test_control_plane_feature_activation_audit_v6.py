from __future__ import annotations

import json

from kun.control_plane import run_feature_activation_audit


def test_feature_activation_audit_runs_trigger_tasks_for_all_core_features(tmp_path) -> None:
    report = run_feature_activation_audit(output_dir=tmp_path / "activation-audit")

    assert report.activated_count == len(report.cases)
    assert report.gap_count == 0
    assert {case.feature_id for case in report.cases} >= {
        "info_gap_human_collaboration",
        "delivery_acceptance_and_state_cleanup",
        "runtime_activation_preflight_checkpoint",
        "worker_pool_resource_lock_wait",
        "parallel_worker_pool_isolated_execution",
        "sqlite_multi_process_worker_pool",
        "redis_distributed_resource_lock_adapter",
        "container_required_blocks_unsupported_runner",
        "qi_nuo_observation_strategy_loop",
        "capability_dedupe_routes_to_qi",
        "merge_conflict_governance",
        "workspace_snapshot_and_rollback",
        "watchtower_runtime_bridge",
        "external_sample_comparison_qi_governance",
        "autonomous_app_development_runner",
        "game_design_research_to_app_runner",
        "game_production_runner_internal_delivery",
        "qi_ab_frontier50_external_runner",
        "productization_dogfood_runner",
    }
    payload = json.loads(
        (tmp_path / "activation-audit" / "feature-activation-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema_version"] == "kun-v6-feature-activation-audit-v1"
    assert payload["real_mission_count"] == 0
    assert payload["fixture_or_synthetic_count"] == len(report.cases)
    assert all(case["trigger_available"] for case in payload["cases"])
    assert all(case["runner_available"] for case in payload["cases"])
    assert not any(case["real_mission_e2e_passed"] for case in payload["cases"])
    markdown = (tmp_path / "activation-audit" / "feature-activation-audit.md").read_text(
        encoding="utf-8"
    )
    assert "Trigger Map" in markdown
    assert "Real E2E" in markdown
