"""Object storage adapter — keep large blobs out of Postgres.

DB stores small structured data; MinIO/S3 stores anything bigger than
``KUN_RESULT_OFFLOAD_THRESHOLD_BYTES`` (default 50 KiB):

  - Task results that include long-form output, generated reports, code dumps
  - User uploads (PDF / Excel / images that pdf-read / csv-query process)
  - Long-term memory binaries (images attached to context assets)

Why MinIO and not "just put bigger TEXT in Postgres"? Two reasons —
DB performance stays predictable as task output sizes vary, and we can
serve files directly to the frontend later without piping them through
the API tier.

The adapter is intentionally minimal: ``put`` returns an opaque ``s3://``
ref string; ``get`` reads it back. Bucket creation is idempotent. All
blocking I/O is dispatched to a thread so async callers don't block the
event loop.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from kun.core.config import settings
from kun.core.logging import get_logger

log = get_logger("kun.object_store")


@dataclass(frozen=True)
class ObjectRef:
    """A reference returned by ``put``; opaque token for ``get``."""

    uri: str  # e.g. "s3://kun-artifacts/task-results/tk-01H../result.json"
    bucket: str
    key: str
    size_bytes: int

    @classmethod
    def from_uri(cls, uri: str, *, size_bytes: int = 0) -> ObjectRef:
        parsed = urlparse(uri)
        if parsed.scheme not in {"s3", "minio"}:
            raise ValueError(f"unsupported uri scheme: {uri}")
        return cls(
            uri=uri, bucket=parsed.netloc, key=parsed.path.lstrip("/"), size_bytes=size_bytes
        )


class ObjectStore:
    """Thin wrapper around the synchronous minio SDK, async-friendly."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        region: str | None = None,
        secure: bool | None = None,
    ) -> None:
        cfg = settings()
        ep = endpoint or cfg.s3_endpoint
        # MinIO client wants host:port without scheme
        parsed = urlparse(ep if "//" in ep else f"http://{ep}")
        self._secure = bool(secure) if secure is not None else parsed.scheme == "https"
        self._endpoint = parsed.netloc or parsed.path
        self._client = Minio(
            self._endpoint,
            access_key=access_key or cfg.s3_access_key,
            secret_key=secret_key or cfg.s3_secret_key,
            region=region or cfg.s3_region,
            secure=self._secure,
        )
        self._bucket = bucket or cfg.s3_bucket
        self._bucket_ready = False

    # ---------- bucket lifecycle ----------

    def _ensure_bucket_sync(self) -> None:
        if self._bucket_ready:
            return
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                log.info("object_store.bucket_created", bucket=self._bucket)
        except S3Error as e:
            # Race: another worker just created it. That's fine.
            if e.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        self._bucket_ready = True

    async def ensure_bucket(self) -> None:
        await asyncio.to_thread(self._ensure_bucket_sync)

    # ---------- put / get ----------

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> ObjectRef:
        await self.ensure_bucket()
        size = len(data)

        def _do() -> None:
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(data),
                length=size,
                content_type=content_type,
            )

        await asyncio.to_thread(_do)
        uri = f"s3://{self._bucket}/{key}"
        log.debug("object_store.put", uri=uri, size_bytes=size)
        return ObjectRef(uri=uri, bucket=self._bucket, key=key, size_bytes=size)

    async def put_json(self, key: str, payload: Any) -> ObjectRef:
        return await self.put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )

    async def get_bytes(self, ref: ObjectRef | str) -> bytes:
        if isinstance(ref, str):
            ref = ObjectRef.from_uri(ref)

        def _do() -> bytes:
            response = self._client.get_object(ref.bucket, ref.key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_do)

    async def get_json(self, ref: ObjectRef | str) -> Any:
        data = await self.get_bytes(ref)
        return json.loads(data.decode("utf-8"))


# ---------- module-level singleton ----------

_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = ObjectStore()
    return _store


def set_object_store(store: ObjectStore) -> None:
    """Override the cached store (tests)."""
    global _store
    _store = store


def reset_object_store() -> None:
    global _store
    _store = None


# ---------- helpers used by orchestrator ----------


def should_offload(payload_bytes: bytes) -> bool:
    """Decide if a result payload should be offloaded based on configured threshold."""
    threshold = settings().result_offload_threshold_bytes
    return threshold > 0 and len(payload_bytes) >= threshold


async def maybe_offload_result_json(
    payload: Any,
    *,
    tenant_id: str,
    task_id: str,
) -> tuple[Any, ObjectRef | None]:
    """Return (payload_for_db, offload_ref).

    If the JSON payload is over the threshold, store it in MinIO and return
    a stub dict that the DB can persist. The full payload can be re-fetched
    via ``get_object_store().get_json(stub["_offload_ref"])``.

    On any object-store failure we fall back to the original payload — never
    drop data on a transient MinIO outage.
    """
    serialized = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if not should_offload(serialized):
        return payload, None

    key = f"task-results/{tenant_id}/{task_id}.json"
    try:
        ref = await get_object_store().put_bytes(
            key,
            serialized,
            content_type="application/json",
        )
    except Exception as e:
        log.warning(
            "object_store.offload_failed_falling_back",
            task_id=task_id,
            error=str(e),
        )
        return payload, None

    stub = {
        "_offload_ref": ref.uri,
        "_offload_size_bytes": ref.size_bytes,
        "_offload_content_type": "application/json",
    }
    return stub, ref


__all__ = [
    "ObjectRef",
    "ObjectStore",
    "get_object_store",
    "maybe_offload_result_json",
    "reset_object_store",
    "set_object_store",
    "should_offload",
]
