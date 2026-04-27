"""Tests for TaskPanorama anchored on-demand expansion (BATCH6 C25)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from kun.core.task_panorama import ModuleResult, TaskPanorama
from kun.engineering.panorama_builder import PanoramaBuilder


async def _collect(panorama: TaskPanorama, task_ref: dict, **kwargs) -> list[ModuleResult]:
    return [module async for module in panorama.build_anchored(task_ref, **kwargs)]


def _panorama() -> TaskPanorama:
    return TaskPanorama(
        task_ref="task-c25",
        tier="full",
        intent_one_sentence="ship panorama anchored expansion",
    )


@dataclass(frozen=True)
class _FakeNeighbor:
    entity_kind: str
    entity_id: str
    relation_type: str
    confidence: float
    hops: int
    via_path: tuple[tuple[str, str], ...]

    @property
    def score(self) -> float:
        return self.confidence / self.hops


class _FakeTraversal:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def neighbors(
        self,
        kind: str,
        entity_id: str,
        *,
        hops: int,
        relation_types=None,
        limit_per_hop: int,
    ) -> list[_FakeNeighbor]:
        self.calls.append(
            {
                "kind": kind,
                "entity_id": entity_id,
                "hops": hops,
                "relation_types": relation_types,
                "limit_per_hop": limit_per_hop,
            }
        )
        if entity_id != "task.intent":
            return []
        return [
            _FakeNeighbor(
                entity_kind="capability_card",
                entity_id="cc-writer",
                relation_type="produced_by",
                confidence=0.8,
                hops=1,
                via_path=(("panorama_module", "task.intent"), ("capability_card", "cc-writer")),
            )
        ]


@pytest.mark.asyncio
async def test_build_anchored_fast_mode_yields_minimal_round() -> None:
    modules = await _collect(
        _panorama(),
        {
            "task_id": "task-fast",
            "execution_mode": "FAST",
            "intent_one_sentence": "answer quickly",
            "risk_level": "low",
        },
    )

    assert [m.module_name for m in modules] == ["intent_one_sentence", "risk_summary"]
    assert {m.round_index for m in modules} == {1}
    assert all(m.required for m in modules)


@pytest.mark.asyncio
async def test_build_anchored_smart_mode_yields_two_rounds() -> None:
    modules = await _collect(
        _panorama(),
        {
            "task_id": "task-smart",
            "execution_mode": "SMART",
            "risk_level": "medium",
            "complexity_score": 0.5,
        },
    )

    assert [m.module_name for m in modules] == [
        "intent_one_sentence",
        "risk_summary",
        "risk_assessment",
        "complexity_score",
    ]
    assert [m.round_index for m in modules] == [1, 1, 2, 2]


@pytest.mark.asyncio
async def test_build_anchored_max_mode_yields_three_rounds() -> None:
    modules = await _collect(
        _panorama(),
        {
            "task_id": "task-max",
            "execution_mode": "MAX",
            "risk_level": "critical",
            "complexity_score": 0.9,
        },
    )

    assert [m.module_name for m in modules] == [
        "intent_one_sentence",
        "risk_summary",
        "risk_assessment",
        "complexity_score",
        "multi_judge_review",
        "cross_check",
        "alternative_paths",
        "risk_graph",
    ]
    assert [m.round_index for m in modules] == [1, 1, 2, 2, 3, 3, 3, 3]


@pytest.mark.asyncio
async def test_build_anchored_max_rounds_caps_mode() -> None:
    modules = await _collect(
        _panorama(),
        {"task_id": "task-cap", "execution_mode": "MAX"},
        max_rounds=2,
    )

    assert [m.module_name for m in modules] == [
        "intent_one_sentence",
        "risk_summary",
        "risk_assessment",
        "complexity_score",
    ]
    assert max(m.round_index for m in modules) == 2


@pytest.mark.asyncio
async def test_build_anchored_caller_can_stop_after_marginal_roi_decision() -> None:
    panorama = _panorama()
    modules: list[ModuleResult] = []

    async for module in panorama.build_anchored({"task_id": "task-stop", "execution_mode": "MAX"}):
        modules.append(module)
        if len(modules) == 2:
            break

    assert [m.module_name for m in modules] == ["intent_one_sentence", "risk_summary"]


@pytest.mark.asyncio
async def test_build_anchored_preserves_stable_output_order() -> None:
    modules = await _collect(
        _panorama(),
        {"task_id": "task-order", "execution_mode": "MAX"},
    )

    positions = {module.module_name: idx for idx, module in enumerate(modules)}
    assert positions["intent_one_sentence"] < positions["risk_summary"]
    assert positions["risk_summary"] < positions["risk_assessment"]
    assert positions["complexity_score"] < positions["multi_judge_review"]
    assert positions["multi_judge_review"] < positions["cross_check"]


@pytest.mark.asyncio
async def test_build_anchored_uses_task_ref_payload_values() -> None:
    modules = await _collect(
        _panorama(),
        {
            "task_id": "task-payload",
            "execution_mode": "SMART",
            "success_criteria_short": "produce a clear summary",
            "risk_level": "high",
            "complexity_score": 0.75,
            "estimated_cost_usd": 0.42,
        },
    )

    by_name = {module.module_name: module for module in modules}
    assert by_name["intent_one_sentence"].payload == {
        "task_ref": "task-payload",
        "intent_one_sentence": "produce a clear summary",
    }
    assert by_name["risk_summary"].payload["risk_level"] == "high"
    assert by_name["risk_assessment"].payload["estimated_cost_usd"] == 0.42
    assert by_name["complexity_score"].payload["complexity_score"] == 0.75


@pytest.mark.asyncio
async def test_build_anchored_uses_graph_traversal_for_module_neighbors() -> None:
    traversal = _FakeTraversal()

    modules = await _collect(
        _panorama(),
        {
            "task_id": "task-graph",
            "execution_mode": "SMART",
            "intent_one_sentence": "use graph context",
        },
        graph_traversal=traversal,
        graph_relation_types=["produced_by"],
        graph_limit_per_hop=3,
    )

    assert traversal.calls[0] == {
        "kind": "panorama_module",
        "entity_id": "task.intent",
        "hops": 1,
        "relation_types": ["produced_by"],
        "limit_per_hop": 3,
    }
    intent_payload = modules[0].payload
    assert intent_payload["graph_neighbors"] == [
        {
            "entity_kind": "capability_card",
            "entity_id": "cc-writer",
            "relation_type": "produced_by",
            "confidence": 0.8,
            "hops": 1,
            "score": 0.8,
            "via_path": [
                {"entity_kind": "panorama_module", "entity_id": "task.intent"},
                {"entity_kind": "capability_card", "entity_id": "cc-writer"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_build_anchored_graph_hops_follow_execution_mode() -> None:
    smart = _FakeTraversal()
    await _collect(_panorama(), {"execution_mode": "SMART"}, graph_traversal=smart)
    assert {call["hops"] for call in smart.calls} == {1}

    max_traversal = _FakeTraversal()
    await _collect(_panorama(), {"execution_mode": "MAX"}, graph_traversal=max_traversal)
    assert {call["hops"] for call in max_traversal.calls} == {2}

    ensemble = _FakeTraversal()
    await _collect(_panorama(), {"execution_mode": "ENSEMBLE"}, graph_traversal=ensemble)
    assert {call["hops"] for call in ensemble.calls} == {3}


@pytest.mark.asyncio
async def test_build_anchored_fast_mode_skips_graph_for_latency() -> None:
    traversal = _FakeTraversal()

    modules = await _collect(
        _panorama(),
        {"task_id": "task-fast-graph", "execution_mode": "FAST"},
        graph_traversal=traversal,
    )

    assert traversal.calls == []
    assert "graph_neighbors" not in modules[0].payload


@pytest.mark.asyncio
async def test_existing_panorama_builder_expand_is_unchanged() -> None:
    builder = PanoramaBuilder()
    panorama = await builder.expand(
        {
            "task_id": "task-old-build",
            "intent_one_sentence": "old path",
            "risk_level": "low",
            "complexity_score": 0.1,
        }
    )

    assert panorama.intent_one_sentence == "old path"
    assert panorama.modules_run == []
    assert panorama.tier == "light"
