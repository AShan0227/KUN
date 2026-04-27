"""Knowledge graph Prometheus metrics + Grafana dashboard sanity (BATCH10 C39)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from kun.context.graph_metrics import emit_relationship_mine_metrics, emit_relationship_snapshot
from kun.context.graph_traversal import GraphTraversal
from kun.core.metrics import (
    graph_traversal_neighbors_count,
    relationship_entity_degree,
    relationship_mine_step_throughput,
    relationships_confidence_p50,
    relationships_total,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "kun" / "infra" / "grafana-dashboard-knowledge-graph.json"


def _metric_sample(collector, sample_name: str, **labels: str) -> float:
    for metric in collector.collect():
        for sample in metric.samples:
            if sample.name != sample_name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return 0.0


def test_relationship_snapshot_updates_total_p50_and_degree() -> None:
    rels = [
        {
            "source_entity_kind": "skill",
            "source_entity_id": "python",
            "target_entity_kind": "task",
            "target_entity_id": "analysis",
            "relation_type": "metrics_snapshot",
            "confidence": 0.3,
        },
        {
            "source_entity_kind": "skill",
            "source_entity_id": "python",
            "target_entity_kind": "asset",
            "target_entity_id": "report",
            "relation_type": "metrics_snapshot",
            "confidence": 0.7,
        },
        {
            "source_entity_kind": "task",
            "source_entity_id": "analysis",
            "target_entity_kind": "asset",
            "target_entity_id": "report",
            "relation_type": "metrics_snapshot",
            "confidence": 0.9,
        },
    ]

    emit_relationship_snapshot(rels)

    assert (
        _metric_sample(
            relationships_total,
            "kun_relationships_total",
            relation_type="metrics_snapshot",
        )
        == 3
    )
    assert (
        _metric_sample(
            relationships_confidence_p50,
            "kun_relationships_confidence_p50",
            relation_type="metrics_snapshot",
        )
        == 0.7
    )
    assert (
        _metric_sample(
            relationship_entity_degree,
            "kun_relationship_entity_degree",
            entity_kind="skill",
            entity_id="python",
        )
        == 2
    )


def test_relationship_mine_metrics_increment_counter() -> None:
    before = _metric_sample(
        relationship_mine_step_throughput,
        "kun_relationship_mine_step_throughput_total",
        relation_type="metrics_mined",
    )

    emit_relationship_mine_metrics(
        [
            {
                "source_entity_kind": "skill",
                "source_entity_id": "python",
                "target_entity_kind": "task",
                "target_entity_id": "analysis",
                "relation_type": "metrics_mined",
                "confidence": 0.7,
            },
            {
                "source_entity_kind": "skill",
                "source_entity_id": "python",
                "target_entity_kind": "asset",
                "target_entity_id": "doc",
                "relation_type": "metrics_mined",
                "confidence": 0.9,
            },
        ]
    )

    after = _metric_sample(
        relationship_mine_step_throughput,
        "kun_relationship_mine_step_throughput_total",
        relation_type="metrics_mined",
    )
    assert after == before + 2


@pytest.mark.asyncio
async def test_graph_traversal_observes_neighbor_count() -> None:
    before_count = _metric_sample(
        graph_traversal_neighbors_count,
        "kun_graph_traversal_neighbors_count_count",
    )
    before_sum = _metric_sample(
        graph_traversal_neighbors_count,
        "kun_graph_traversal_neighbors_count_sum",
    )

    tr = GraphTraversal()
    tr._fetch_edges = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "target_kind": "asset",
                "target_id": "a-2",
                "relation_type": "depends_on",
                "confidence": 0.9,
            },
            {
                "target_kind": "asset",
                "target_id": "a-3",
                "relation_type": "mentions",
                "confidence": 0.5,
            },
        ]
    )

    neighbors = await tr.neighbors("asset", "a-1", hops=1)

    assert len(neighbors) == 2
    after_count = _metric_sample(
        graph_traversal_neighbors_count,
        "kun_graph_traversal_neighbors_count_count",
    )
    after_sum = _metric_sample(
        graph_traversal_neighbors_count,
        "kun_graph_traversal_neighbors_count_sum",
    )
    assert after_count == before_count + 1
    assert after_sum == before_sum + 2


def test_knowledge_graph_dashboard_json_valid() -> None:
    assert DASHBOARD_PATH.exists(), f"missing dashboard: {DASHBOARD_PATH}"
    data = json.loads(DASHBOARD_PATH.read_text())

    assert data["title"] == "KUN Knowledge Graph (V2.2 §20)"
    assert data["uid"] == "kun-knowledge-graph"
    assert len(data["panels"]) >= 6


def test_knowledge_graph_dashboard_references_expected_metrics() -> None:
    data = json.loads(DASHBOARD_PATH.read_text())
    text = json.dumps(data)

    expected = {
        "kun_relationships_total",
        "kun_relationships_confidence_p50",
        "kun_relationship_mine_step_throughput",
        "kun_relationship_entity_degree",
        "kun_graph_traversal_neighbors_count_bucket",
        "kun_graph_traversal_neighbors_count_count",
    }
    for metric in expected:
        assert metric in text, f"dashboard missing reference to {metric}"
