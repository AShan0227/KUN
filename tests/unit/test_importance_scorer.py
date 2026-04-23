"""Importance scorer tests."""

from datetime import UTC, datetime, timedelta

import pytest
from kun.context.importance import ImportanceScorer


@pytest.mark.unit
def test_relevance_bounds():
    s = ImportanceScorer()
    assert s.relevance(1.0) == 1.0
    assert s.relevance(-1.0) == 0.0
    assert 0.49 < s.relevance(0.0) < 0.51


@pytest.mark.unit
def test_frequency_saturation():
    s = ImportanceScorer(freq_saturation_k=10.0)
    # At k=10, freq=10 gives 0.5
    assert abs(s.frequency(10) - 0.5) < 1e-9
    # Large count → 1.0 asymptote
    assert s.frequency(10_000) > 0.99


@pytest.mark.unit
def test_recency_half_life():
    s = ImportanceScorer(recency_decay_days=30.0)
    now = datetime.now(UTC)
    assert abs(s.recency(now - timedelta(days=0), now) - 1.0) < 1e-9
    # After half-life → 0.5
    assert abs(s.recency(now - timedelta(days=30), now) - 0.5) < 1e-3


@pytest.mark.unit
def test_compound_score_bounded():
    s = ImportanceScorer()
    sc = s.score(
        relevance_cos=0.8,
        access_count=20,
        last_access=datetime.now(UTC) - timedelta(days=5),
    )
    assert 0 <= sc.value <= 1
    assert sc.kind == "importance"
    assert set(sc.components) == {"relevance", "frequency", "recency"}
