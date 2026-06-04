"""Qdrant-backed importance scoring smoke tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import qdrant_embed_text
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore, get_qdrant_client
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from qdrant_client import models


def _task() -> TaskRef:
    owner = Owner(tenant_id="u-qdrant")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("postgres rls migration", owner),
        task_type="coding.database.postgres",
        owner=owner,
        success_criteria_short="修复 Postgres RLS tenant isolation",
    )
    spec = TaskSpec(
        goal_detail="需要审查 postgres tenant rls migration 并给出修复方案",
        success_metrics=["tenant isolation 正确"],
        required_skills=["database-postgres"],
    )
    return TaskRef(meta=meta, spec=spec)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qdrant_client_and_importance_sorting() -> None:
    client = get_qdrant_client()
    try:
        await asyncio.to_thread(client.get_collections)
    except Exception as exc:
        pytest.skip(f"qdrant is not available: {exc!r}")

    collection_name = f"kun_test_importance_{uuid4().hex}"
    query_vector = qdrant_embed_text("postgres tenant rls")
    relevant_vector = qdrant_embed_text("postgres tenant rls migration")
    unrelated_vector = qdrant_embed_text("email marketing campaign")

    await asyncio.to_thread(
        client.create_collection,
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=len(query_vector), distance=models.Distance.COSINE),
    )
    try:
        relevant_id = str(uuid4())
        unrelated_id = str(uuid4())
        await asyncio.to_thread(
            client.upsert,
            collection_name=collection_name,
            points=[
                models.PointStruct(
                    id=relevant_id,
                    vector=relevant_vector,
                    payload={"asset_id": "relevant"},
                ),
                models.PointStruct(
                    id=unrelated_id,
                    vector=unrelated_vector,
                    payload={"asset_id": "unrelated"},
                ),
            ],
        )
        qdrant_hits = await asyncio.to_thread(
            client.query_points,
            collection_name=collection_name,
            query=query_vector,
            limit=2,
        )

        assert qdrant_hits.points[0].payload == {"asset_id": "relevant"}

        store = InMemoryAssetStore()
        relevant = LayeredAsset.build(
            "knowledge",
            "u-qdrant",
            metadata={"title": "RLS migration"},
            summary="postgres tenant rls migration",
            tags=["postgres", "rls"],
        )
        unrelated = LayeredAsset.build(
            "knowledge",
            "u-qdrant",
            metadata={"title": "Marketing"},
            summary="email marketing campaign",
            tags=["marketing"],
        )
        await store.put(unrelated)
        await store.put(relevant)

        pack = await ContextPacker(store).pack(_task(), tenant_id="u-qdrant")

        assert pack.items
        assert pack.items[0].asset_id == relevant.asset_id
    finally:
        with suppress(Exception):
            await asyncio.to_thread(client.delete_collection, collection_name)
