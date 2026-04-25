"""Seed the AssetStore with hand-written knowledge on cold start.

Without this, ``ContextPacker`` returns an empty list every time and the
"context preheat" stage of the orchestrator is a no-op. The seed YAML
(``seeds/context_assets.yaml``) is the **lower bound** of what the agent
should always have available; new assets accumulate on top from real
task outcomes (idle-batch methodology distill).

The seeder is idempotent — it skips if the tenant already has any assets,
so reload after edit is: clear store first, then call ``seed_default()``,
or use ``kun context seed --force``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from kun.context.assets import AssetKind, LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.core.logging import get_logger

log = get_logger("kun.context.seeds")


_DEFAULT_SEED_PATH = Path(__file__).resolve().parents[2] / "seeds" / "context_assets.yaml"


def _load_yaml(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        log.info("context.seeds.absent", path=str(path))
        return []
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("context.seeds.yaml_invalid", path=str(path), error=str(e))
        return []
    raw: list[dict[str, Any]] = data.get("assets") or []
    return raw


def _row_to_asset(row: dict[str, Any], tenant_id: str) -> LayeredAsset | None:
    try:
        kind: AssetKind = row["kind"]
        summary = str(row.get("summary") or "").strip()
        return LayeredAsset.build(
            asset_kind=kind,
            tenant_id=tenant_id,
            metadata=row.get("metadata") or {},
            summary=summary,
            tags=list(row.get("tags") or []),
        )
    except (KeyError, TypeError) as e:
        log.warning("context.seeds.row_invalid", error=str(e), row=row)
        return None


async def seed_default(
    *,
    tenant_id: str,
    path: Path | None = None,
    store: AssetStore | None = None,
    force: bool = False,
) -> int:
    """Seed the default asset set for a tenant.

    Returns the number of assets inserted. Skips with 0 when the tenant
    already has assets unless ``force=True``.
    """
    target_store = store or get_store()
    yaml_path = path or _DEFAULT_SEED_PATH

    if not force:
        existing = await target_store.list(tenant_id=tenant_id, limit=1)
        if existing:
            log.info(
                "context.seeds.skip_already_seeded",
                tenant_id=tenant_id,
                existing_count=len(existing),
            )
            return 0

    rows = _load_yaml(yaml_path)
    inserted = 0
    for row in rows:
        asset = _row_to_asset(row, tenant_id)
        if asset is None:
            continue
        await target_store.put(asset)
        inserted += 1

    log.info("context.seeds.loaded", tenant_id=tenant_id, count=inserted, path=str(yaml_path))
    return inserted


async def seed_from_iterable(
    rows: Iterable[dict[str, Any]],
    *,
    tenant_id: str,
    store: AssetStore | None = None,
) -> int:
    """Insert a programmatically-built batch (used by tests / migrations)."""
    target_store = store or get_store()
    inserted = 0
    for row in rows:
        asset = _row_to_asset(row, tenant_id)
        if asset is None:
            continue
        await target_store.put(asset)
        inserted += 1
    return inserted


__all__ = ["seed_default", "seed_from_iterable"]
