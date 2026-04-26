"""Tests for V2.1 data models: TaskPanorama / AttentionAnchor / EmergentSolution / variable_registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.core.attention_anchor import (
    AttentionAnchor,
    AttentionManager,
    get_manager,
    reset_manager,
)
from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
    get_library,
    reset_library,
)
from kun.core.task_panorama import (
    AttentionAllocation,
    ContextPreheat,
    PanoramaPatch,
    RiskAssessment,
    StepPlan,
    TaskPanorama,
)
from kun.core.variable_registry import (
    ENV_VARS,
    HISTORY_VARS,
    META_VARS,
    RESOURCE_VARS,
    SYSTEM_VARS,
    TASK_VARS,
    USER_VARS,
    all_names,
    get,
    list_by_family,
)

# ---- variable_registry tests ----


def test_variable_registry_size() -> None:
    """V2.1 §17.7 承诺 62 变量谱."""
    assert len(TASK_VARS) == 12, "task vars 漏"
    assert len(USER_VARS) == 13, "user vars 漏"
    assert len(RESOURCE_VARS) == 8
    assert len(SYSTEM_VARS) == 10
    assert len(HISTORY_VARS) == 8
    assert len(ENV_VARS) == 6
    assert len(META_VARS) == 5
    total = (
        len(TASK_VARS)
        + len(USER_VARS)
        + len(RESOURCE_VARS)
        + len(SYSTEM_VARS)
        + len(HISTORY_VARS)
        + len(ENV_VARS)
        + len(META_VARS)
    )
    assert total == 62


def test_variable_registry_get() -> None:
    spec = get("risk_level")
    assert spec.family == "task"
    assert "all_decision_kinds" in spec.decision_uses


def test_variable_registry_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get("not_a_var")


def test_list_by_family() -> None:
    task_specs = list_by_family("task")
    assert len(task_specs) == 12
    names = {s.name for s in task_specs}
    assert "task_type" in names
    assert "complexity_score" in names
    assert "risk_level" in names


def test_all_names_unique() -> None:
    names = all_names()
    assert len(names) == len(set(names)), "变量名重复"
    assert len(names) == 62


# ---- TaskPanorama tests ----


def test_panorama_minimal_tier() -> None:
    p = TaskPanorama(
        task_ref="tk-001",
        tier="minimal",
        intent_one_sentence="echo hello",
    )
    assert p.tier == "minimal"
    assert p.execution_plan == []
    assert p.context_preheat is None
    assert p.risk_assessment is None


def test_panorama_full_tier_with_subfields() -> None:
    p = TaskPanorama(
        task_ref="tk-002",
        tier="full",
        intent_one_sentence="重构整个项目",
        execution_plan=[
            StepPlan(step_index=0, intent="解析需求"),
            StepPlan(step_index=1, depends_on=[0], intent="拆解"),
        ],
        risk_assessment=RiskAssessment(
            financial_risk=0.3,
            irreversibility_risk=0.7,
            complexity_risk=0.9,
            overall_risk_level="critical",
        ),
        context_preheat=ContextPreheat(
            pinned_assets=["a1", "a2"],
            depth="deep",
        ),
        attention_allocation=AttentionAllocation(
            importance=0.8,
            complexity=0.9,
            chosen_model_tier="top",
        ),
    )
    assert p.tier == "full"
    assert len(p.execution_plan) == 2
    assert p.risk_assessment.overall_risk_level == "critical"
    assert p.context_preheat.depth == "deep"
    assert p.attention_allocation.chosen_model_tier == "top"


def test_panorama_patch_history() -> None:
    p = TaskPanorama(task_ref="tk-003", tier="medium", intent_one_sentence="x")
    p.patches.append(
        PanoramaPatch(
            patch_id="p1",
            patched_at=datetime.now(UTC),
            reason="emergent_solution_swap",
            patch_kind="node_replace",
            affected_nodes=[1, 2],
        )
    )
    assert len(p.patches) == 1
    assert p.patches[0].reason == "emergent_solution_swap"


# ---- AttentionAnchor tests ----


def test_anchor_basic() -> None:
    a = AttentionAnchor(
        anchor_kind="user_pin",
        target_asset_ref="ka-1",
        weight_boost=0.2,
        user_id="u-1",
    )
    assert a.anchor_kind == "user_pin"
    assert a.weight_boost == 0.2


def test_anchor_weight_capped() -> None:
    with pytest.raises(ValueError):
        AttentionAnchor(
            anchor_kind="user_pin",
            target_asset_ref="ka-1",
            weight_boost=0.6,  # > 0.5 上限
        )


def test_attention_manager_add_remove() -> None:
    m = AttentionManager()
    a = AttentionAnchor(
        anchor_kind="user_pin",
        target_asset_ref="ka-1",
        user_id="u-1",
    )
    m.add(a)
    assert m.get(a.anchor_id) is a
    assert m.remove(a.anchor_id) is True
    assert m.get(a.anchor_id) is None


def test_attention_manager_list_for_user() -> None:
    m = AttentionManager()
    m.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="a", user_id="u1"))
    m.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="b", user_id="u2"))
    m.add(AttentionAnchor(anchor_kind="permanent_redline", target_asset_ref="c"))
    out = m.list_for_user("u1")
    assert len(out) == 2  # u1 自己的 + 无 user_id 的全局
    assert any(a.target_asset_ref == "a" for a in out)
    assert any(a.target_asset_ref == "c" for a in out)


def test_attention_manager_expired_anchor_filtered() -> None:
    m = AttentionManager()
    past = datetime.now(UTC) - timedelta(days=1)
    m.add(
        AttentionAnchor(
            anchor_kind="user_pin",
            target_asset_ref="expired",
            user_id="u1",
            expires_at=past,
        )
    )
    out = m.list_for_user("u1")
    assert len(out) == 0


def test_attention_manager_boost_for_asset() -> None:
    m = AttentionManager()
    m.add(
        AttentionAnchor(
            anchor_kind="user_pin", target_asset_ref="ka-1", weight_boost=0.15, user_id="u1"
        )
    )
    m.add(
        AttentionAnchor(anchor_kind="permanent_redline", target_asset_ref="ka-1", weight_boost=0.30)
    )
    boost = m.boost_for_asset("ka-1", user_id="u1")
    # 取最大, 不累加
    assert boost == 0.30


def test_attention_manager_must_check_for_decision() -> None:
    m = AttentionManager()
    m.add(AttentionAnchor(anchor_kind="user_pin", target_asset_ref="a"))
    m.add(AttentionAnchor(anchor_kind="permanent_redline", target_asset_ref="b"))
    out = m.must_check_for_decision("model_select")
    kinds = {a.anchor_kind for a in out}
    assert "user_pin" in kinds
    assert "permanent_redline" in kinds


def test_attention_manager_singleton() -> None:
    reset_manager()
    m1 = get_manager()
    m2 = get_manager()
    assert m1 is m2
    reset_manager()


# ---- EmergentSolution tests ----


def test_emergent_solution_basic() -> None:
    sol = EmergentSolution(
        task_type="coding.python.fastapi",
        discovered_by="external_scan",
        source=EmergentSource(kind="github_issue", url="https://...", snippet="..."),
        description="用 SQLModel",
        estimated_outcome_delta=0.05,
        estimated_cost_delta=-0.30,
    )
    assert sol.status == "candidate"
    assert sol.discovered_by == "external_scan"


def test_emergent_library_add_get() -> None:
    lib = EmergentSolutionLibrary()
    sol = EmergentSolution(
        task_type="x.y",
        discovered_by="llm_metacognitive",
        source=EmergentSource(kind="llm_judgment"),
    )
    lib.add(sol)
    assert lib.get(sol.solution_id) is sol


def test_emergent_library_list_for_task_type_hierarchy() -> None:
    lib = EmergentSolutionLibrary()
    s1 = EmergentSolution(
        task_type="coding",
        discovered_by="external_scan",
        source=EmergentSource(kind="reddit"),
    )
    s2 = EmergentSolution(
        task_type="coding.python",
        discovered_by="external_scan",
        source=EmergentSource(kind="reddit"),
    )
    s3 = EmergentSolution(
        task_type="writing",
        discovered_by="external_scan",
        source=EmergentSource(kind="reddit"),
    )
    lib.add(s1)
    lib.add(s2)
    lib.add(s3)
    out = lib.list_for_task_type("coding.python.fastapi")
    # s1 (coding) 和 s2 (coding.python) 都匹配
    ids = {s.solution_id for s in out}
    assert s1.solution_id in ids
    assert s2.solution_id in ids
    assert s3.solution_id not in ids


def test_emergent_library_promote_reject() -> None:
    lib = EmergentSolutionLibrary()
    sol = EmergentSolution(
        task_type="x",
        discovered_by="external_scan",
        source=EmergentSource(kind="reddit"),
    )
    lib.add(sol)
    assert lib.promote(sol.solution_id, "shadow_testing") is True
    assert lib.get(sol.solution_id).status == "shadow_testing"
    assert lib.promote(sol.solution_id, "canary") is True
    assert lib.get(sol.solution_id).promoted_to_canary_at is not None
    assert lib.reject(sol.solution_id, "tested poorly") is True
    assert lib.get(sol.solution_id).status == "rejected"


def test_emergent_library_singleton() -> None:
    reset_library()
    lib1 = get_library()
    lib2 = get_library()
    assert lib1 is lib2
    reset_library()
