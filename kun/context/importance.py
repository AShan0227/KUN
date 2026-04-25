"""中央重要度打分器。

默认走统一的 ``qdrant_embed_text`` embedding 入口；外部 provider 不可用时，
自动回退到稳定的本地词项向量，主流程不会因为 embedding 服务抖动中断。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal

import httpx
from openai import OpenAI
from qdrant_client import QdrantClient

from kun.context.assets import LayeredAsset
from kun.context.storage import get_qdrant_client
from kun.core.config import settings
from kun.core.logging import get_logger
from kun.core.scoring import ScoreDescriptor

EmbedText = Callable[[str], Sequence[float]]
ImportanceTier = Literal["permanent", "long", "short"]

log = get_logger("kun.context.importance")

DEFAULT_WEIGHTS = {
    "semantic": 0.5,
    "frequency": 0.3,
    "recency": 0.2,
}
FREQUENCY_SATURATION_COUNT = 100
LONG_HALF_LIFE_DAYS = 11.25
SHORT_HALF_LIFE_DAYS = 5.0
LOCAL_EMBEDDING_DIMS = 128
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_VOYAGE_EMBEDDING_MODEL = "voyage-3-large"


@dataclass(frozen=True)
class ImportanceScore:
    """Context 资产重要度分数。"""

    overall: float
    semantic: float
    frequency: float
    recency: float
    rationale: str


class ImportanceScorer:
    """按语义相关度、访问频率、近期性给资产打分。"""

    def __init__(
        self,
        *,
        embed_text: EmbedText | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._embed_text = embed_text or qdrant_embed_text
        self.weights = _normalize_weights(weights or DEFAULT_WEIGHTS)

    def score(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ImportanceScore:
        """给一个资产打 0..1 的重要度分。

        query 为空时，说明调用方只想按资产自身热度排序，semantic 直接给 1。
        query 不为空时，优先走注入的 embedding 函数；没有 embedding 时用本地词项相似度兜底。
        """
        now = now or datetime.now(UTC)
        semantic = self.semantic(asset=asset, query=query)
        frequency = self.frequency(asset.access_count)
        recency = self.recency(asset=asset, now=now)
        overall = _clamp01(
            self.weights["semantic"] * semantic
            + self.weights["frequency"] * frequency
            + self.weights["recency"] * recency
        )
        return ImportanceScore(
            overall=overall,
            semantic=semantic,
            frequency=frequency,
            recency=recency,
            rationale=(
                f"semantic={semantic:.3f}, frequency={frequency:.3f}, recency={recency:.3f}; "
                f"weights={self.weights}"
            ),
        )

    def semantic(self, *, asset: LayeredAsset, query: str | None = None) -> float:
        """资产和 query 的语义相关度。"""
        if not query:
            return 1.0

        asset_text = _asset_text(asset)
        if not asset_text.strip():
            return 0.0

        if self._embed_text is qdrant_embed_text and settings().embedding_provider == "local":
            return _lexical_similarity(query, asset_text)

        if self._embed_text is not None:
            try:
                return _cosine_score(self._embed_text(query), self._embed_text(asset_text))
            except (TypeError, ValueError, ZeroDivisionError):
                # embedding 服务或注入函数异常时，回到本地兜底，不让打分器中断主流程。
                pass

        return _lexical_similarity(query, asset_text)

    @staticmethod
    def frequency(access_count: int) -> float:
        """访问频率分：100 次左右饱和到 1。"""
        n = max(0, access_count)
        return _clamp01(math.log1p(n) / math.log1p(FREQUENCY_SATURATION_COUNT))

    def recency(self, *, asset: LayeredAsset, now: datetime | None = None) -> float:
        """近期性分：按资产层级使用不同半衰期。"""
        half_life = half_life_days(asset)
        if half_life is None:
            return 1.0

        now = now or datetime.now(UTC)
        elapsed_days = (now - asset.last_accessed).total_seconds() / 86400
        if elapsed_days <= 0:
            return 1.0
        return _clamp01(math.exp(-elapsed_days / half_life))

    def score_descriptor(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ScoreDescriptor:
        """兼容现有统一打分展示层。"""
        score = self.score(asset=asset, query=query, now=now)
        half_life = half_life_days(asset)
        return ScoreDescriptor(
            kind="importance",
            value=score.overall,
            components={
                "semantic": score.semantic,
                "frequency": score.frequency,
                "recency": score.recency,
            },
            weights=self.weights,
            sample_size=max(0, asset.access_count),
            decay_half_life_days=None if half_life is None else round(half_life),
        )

    def review_needed(self, *, asset: LayeredAsset, score: ImportanceScore) -> bool:
        """判断是否需要后续便宜模型复审。

        这里只返回信号，不在打分器里直接调模型或写事件，避免 T1 污染主流程。
        """
        repeatedly_used_but_low = asset.access_count >= 10 and score.overall < 0.25
        long_unvisited_but_kept = asset.access_count == 0 and score.recency < 0.05
        return repeatedly_used_but_low or long_unvisited_but_kept


def half_life_days(asset: LayeredAsset) -> float | None:
    """根据资产元数据/类型得到半衰期。None 表示永久档。"""
    tier = _importance_tier(asset)
    if tier == "permanent":
        return None
    if tier == "short":
        return SHORT_HALF_LIFE_DAYS
    return LONG_HALF_LIFE_DAYS


@lru_cache(maxsize=4096)
def qdrant_embed_text(text: str) -> list[float]:
    """Embed text for context importance scoring.

    Provider selection is controlled by ``KUN_EMBEDDING_PROVIDER``:

    - ``local`` (default): stable local bag-of-terms vector; zero network cost.
    - ``openai``: OpenAI/OFOx-compatible embeddings API.
    - ``voyage``: Voyage embeddings API.
    - ``qdrant_fastembed``: Qdrant client's FastEmbed integration when installed.

    The function name is intentionally Qdrant-oriented because Qdrant is the
    vector-store boundary. Embedding providers may vary, but callers should not
    care which one is active.
    """
    cleaned = text.strip()
    if not cleaned:
        return [0.0] * LOCAL_EMBEDDING_DIMS

    cfg = settings()
    provider = cfg.embedding_provider
    model = cfg.embedding_model

    try:
        if provider == "openai":
            return _openai_embed_text(cleaned, model or DEFAULT_OPENAI_EMBEDDING_MODEL)
        if provider == "voyage":
            return _voyage_embed_text(cleaned, model or DEFAULT_VOYAGE_EMBEDDING_MODEL)
        if provider == "qdrant_fastembed":
            return _qdrant_fastembed_text(cleaned, model)
    except Exception as exc:
        log.warning(
            "importance.embedding.fallback",
            provider=provider,
            model=model,
            error=repr(exc),
        )

    return _local_embed_text(cleaned)


def _openai_embed_text(text: str, model: str) -> list[float]:
    cfg = settings()
    api_key = os.getenv("KUN_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url: str | None = None
    if api_key is None and cfg.ofox_api_key:
        api_key = cfg.ofox_api_key
        base_url = f"{cfg.ofox_proxy_url.rstrip('/')}/openai/v1"
    if not api_key:
        raise ValueError("OpenAI embedding provider selected but no API key is configured")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=cfg.embedding_timeout_sec)
    response = client.embeddings.create(model=model, input=text)
    return list(response.data[0].embedding)


def _voyage_embed_text(text: str, model: str) -> list[float]:
    api_key = os.getenv("KUN_VOYAGE_API_KEY") or os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise ValueError("Voyage embedding provider selected but no API key is configured")

    response = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": [text]},
        timeout=settings().embedding_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    return [float(value) for value in payload["data"][0]["embedding"]]


def _qdrant_fastembed_text(text: str, model: str | None) -> list[float]:
    client = get_qdrant_client()
    embedding_model = model or QdrantClient.DEFAULT_EMBEDDING_MODEL
    documents = client._embed_documents([text], embedding_model_name=embedding_model)
    _document, vector = next(iter(documents))
    return [float(value) for value in vector]


def _local_embed_text(text: str) -> list[float]:
    """Stable local fallback that preserves existing lexical behavior."""
    vector = [0.0] * LOCAL_EMBEDDING_DIMS
    terms = _terms(text)
    if not terms:
        terms = {text.lower()}

    for term in terms:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % LOCAL_EMBEDDING_DIMS
        weight = 1.0 + min(len(term), 20) / 20.0
        vector[index] += weight

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _importance_tier(asset: LayeredAsset) -> ImportanceTier:
    raw = str(
        asset.l1_metadata.get("importance_tier")
        or asset.l1_metadata.get("retention_tier")
        or asset.l1_metadata.get("tier")
        or ""
    ).lower()
    tags = {tag.lower() for tag in asset.tags}
    if raw in {"permanent", "tier0", "forever"} or tags & {"permanent", "tier0"}:
        return "permanent"
    if raw in {"short", "short_term", "temporary"} or asset.asset_kind in {"task", "handoff"}:
        return "short"
    return "long"


def _asset_text(asset: LayeredAsset) -> str:
    parts: list[str] = [
        asset.asset_kind,
        " ".join(asset.tags),
        asset.l2_summary or "",
        " ".join(f"{key} {value}" for key, value in asset.l1_metadata.items()),
    ]
    return " ".join(parts)


def _cosine_score(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    cosine = dot / (left_norm * right_norm)
    return _clamp01(cosine)


def _lexical_similarity(query: str, asset_text: str) -> float:
    query_terms = _terms(query)
    asset_terms = _terms(asset_text)
    if not query_terms or not asset_terms:
        return 0.0
    overlap = len(query_terms & asset_terms)
    return _clamp01(overlap / math.sqrt(len(query_terms) * len(asset_terms)))


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[\w.-]+", text) if len(part) >= 2}


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    keys = {"semantic", "frequency", "recency"}
    if set(weights) != keys:
        raise ValueError(f"importance weights must be exactly {sorted(keys)}")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("importance weights must sum to a positive number")
    return {key: value / total for key, value in weights.items()}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "ImportanceScore",
    "ImportanceScorer",
    "half_life_days",
    "qdrant_embed_text",
]
