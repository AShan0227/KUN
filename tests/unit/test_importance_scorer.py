"""中央重要度打分器测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import (
    FREQUENCY_SATURATION_COUNT,
    ImportanceScore,
    ImportanceScorer,
    half_life_days,
    qdrant_embed_text,
)
from kun.context.storage import InMemoryAssetStore


def _asset(
    *,
    kind: str = "memory",
    summary: str = "Postgres RLS tenant isolation notes",
    tags: list[str] | None = None,
    access_count: int = 0,
    last_accessed: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> LayeredAsset:
    return LayeredAsset.build(
        asset_kind=kind,  # type: ignore[arg-type]
        tenant_id="u-test",
        summary=summary,
        tags=tags or [],
        metadata=metadata or {"title": "RLS guide"},
    ).model_copy(
        update={
            "access_count": access_count,
            "last_accessed": last_accessed or datetime.now(UTC),
        }
    )


@pytest.mark.unit
def test_score_returns_brief_interface_shape() -> None:
    score = ImportanceScorer().score(asset=_asset(), query="postgres tenant rls")

    assert isinstance(score, ImportanceScore)
    assert 0 <= score.overall <= 1
    assert 0 <= score.semantic <= 1
    assert 0 <= score.frequency <= 1
    assert 0 <= score.recency <= 1
    assert "semantic=" in score.rationale


@pytest.mark.unit
def test_query_none_treats_semantic_as_fully_relevant() -> None:
    score = ImportanceScorer().score(asset=_asset(summary="completely unrelated"), query=None)

    assert score.semantic == 1.0


@pytest.mark.unit
def test_frequency_uses_log_saturation_at_100_accesses() -> None:
    scorer = ImportanceScorer()

    assert scorer.frequency(0) == 0.0
    assert scorer.frequency(FREQUENCY_SATURATION_COUNT) == 1.0
    assert scorer.frequency(10_000) == 1.0


@pytest.mark.unit
def test_recency_uses_asset_half_life() -> None:
    now = datetime.now(UTC)
    scorer = ImportanceScorer()

    long_asset = _asset(last_accessed=now - timedelta(days=11.25))
    short_asset = _asset(kind="task", last_accessed=now - timedelta(days=5))

    assert abs(scorer.recency(asset=long_asset, now=now) - 0.3679) < 0.01
    assert abs(scorer.recency(asset=short_asset, now=now) - 0.3679) < 0.01


@pytest.mark.unit
def test_permanent_assets_do_not_decay() -> None:
    old = datetime.now(UTC) - timedelta(days=10_000)
    asset = _asset(last_accessed=old, metadata={"importance_tier": "permanent"})

    assert half_life_days(asset) is None
    assert ImportanceScorer().recency(asset=asset) == 1.0


@pytest.mark.unit
def test_embedding_similarity_path_is_used_when_injected() -> None:
    def embed_text(text: str) -> list[float]:
        if "postgres" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]

    scorer = ImportanceScorer(embed_text=embed_text)

    relevant = scorer.score(asset=_asset(summary="postgres guide"), query="postgres")
    unrelated = scorer.score(asset=_asset(summary="email marketing"), query="postgres")

    assert relevant.semantic > unrelated.semantic


@pytest.mark.unit
def test_default_qdrant_embedder_keeps_local_term_similarity() -> None:
    scorer = ImportanceScorer()

    relevant = scorer.score(asset=_asset(summary="postgres tenant rls"), query="postgres rls")
    unrelated = scorer.score(
        asset=_asset(summary="email marketing", metadata={"title": "marketing guide"}),
        query="postgres rls",
    )

    assert relevant.semantic > unrelated.semantic
    assert unrelated.semantic == 0.0


@pytest.mark.unit
def test_qdrant_embed_text_is_stable_without_external_provider() -> None:
    qdrant_embed_text.cache_clear()

    first = qdrant_embed_text("postgres tenant rls")
    second = qdrant_embed_text("postgres tenant rls")

    assert first == second
    assert len(first) == 128


@pytest.mark.unit
def test_embedding_failure_falls_back_to_local_text_similarity() -> None:
    def broken_embedder(_text: str) -> list[float]:
        raise ValueError("embedding service down")

    score = ImportanceScorer(embed_text=broken_embedder).score(
        asset=_asset(summary="tenant rls postgres"),
        query="tenant rls",
    )

    assert score.semantic > 0


@pytest.mark.unit
def test_score_descriptor_keeps_existing_display_contract() -> None:
    asset = _asset(access_count=3)
    descriptor = ImportanceScorer().score_descriptor(asset=asset, query="postgres")

    assert descriptor.kind == "importance"
    assert 0 <= descriptor.value <= 1
    assert descriptor.sample_size == 3
    assert set(descriptor.components) == {"semantic", "frequency", "recency"}


@pytest.mark.unit
def test_review_needed_for_repeatedly_used_but_low_score() -> None:
    asset = _asset(summary="marketing copy", access_count=15)
    score = ImportanceScorer(weights={"semantic": 1.0, "frequency": 0.0, "recency": 0.0}).score(
        asset=asset,
        query="postgres rls migration",
    )

    assert ImportanceScorer().review_needed(asset=asset, score=score) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_assets_sort_by_importance() -> None:
    store = InMemoryAssetStore()
    now = datetime.now(UTC)
    assets = [
        _asset(summary="postgres rls tenant migration", access_count=20, last_accessed=now),
        _asset(
            summary="postgres rls old note", access_count=1, last_accessed=now - timedelta(days=30)
        ),
        _asset(summary="frontend button polish", access_count=100, last_accessed=now),
        _asset(summary="tenant isolation checklist", access_count=3, last_accessed=now),
        _asset(summary="random scratch", access_count=0, last_accessed=now - timedelta(days=60)),
    ]
    for asset in assets:
        await store.put(asset)

    scorer = ImportanceScorer()
    scored = [
        (scorer.score(asset=asset, query="postgres tenant rls", now=now).overall, asset)
        for asset in await store.list(tenant_id="u-test")
    ]
    scored.sort(key=lambda item: item[0], reverse=True)

    assert scored[0][1].l2_summary == "postgres rls tenant migration"
    assert scored[-1][1].l2_summary == "random scratch"
