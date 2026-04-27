"""Prometheus metric helpers for the tenant-scoped knowledge graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from statistics import median
from typing import Any

from kun.core.metrics import (
    relationship_entity_degree,
    relationship_mine_step_throughput,
    relationships_confidence_p50,
    relationships_total,
)


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def emit_relationship_snapshot(relationships: Iterable[Any]) -> None:
    """Emit a best-effort snapshot for relationship inventory dashboards.

    This helper intentionally accepts both EntityRelationship models and dumped
    payload dictionaries so mining, API, and future batch refresh paths can use
    the same metric logic.
    """
    by_type: dict[str, list[float]] = defaultdict(list)
    degree: Counter[tuple[str, str]] = Counter()

    for rel in relationships:
        relation_type = str(_field(rel, "relation_type", "unknown"))
        confidence = float(_field(rel, "confidence", 0.0) or 0.0)
        by_type[relation_type].append(confidence)

        source_kind = _field(rel, "source_entity_kind")
        source_id = _field(rel, "source_entity_id")
        target_kind = _field(rel, "target_entity_kind")
        target_id = _field(rel, "target_entity_id")
        if source_kind and source_id:
            degree[(str(source_kind), str(source_id))] += 1
        if target_kind and target_id:
            degree[(str(target_kind), str(target_id))] += 1

    for relation_type, confidences in by_type.items():
        relationships_total.labels(relation_type=relation_type).set(len(confidences))
        relationships_confidence_p50.labels(relation_type=relation_type).set(
            float(median(confidences))
        )

    for (entity_kind, entity_id), count in degree.items():
        relationship_entity_degree.labels(entity_kind=entity_kind, entity_id=entity_id).set(count)


def emit_relationship_mine_metrics(relationships: Iterable[Any]) -> None:
    """Increment RelationshipMineStep throughput and refresh snapshot gauges."""
    rels = list(relationships)
    by_type: Counter[str] = Counter(str(_field(rel, "relation_type", "unknown")) for rel in rels)
    for relation_type, count in by_type.items():
        relationship_mine_step_throughput.labels(relation_type=relation_type).inc(count)
    emit_relationship_snapshot(rels)


__all__ = [
    "emit_relationship_mine_metrics",
    "emit_relationship_snapshot",
]
