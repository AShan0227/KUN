"""ExternalInfoScanner — 外部信息饱和度监控 (V2.1 §3.10 / 漏洞 3+16).

异步守望驱动 (不阻塞主路径). idle 周期跑外部检索, 把候选方案写到 EmergentSolution 库.

5 关键设计:
- 永不阻塞主路径 (任务执行 critical path 上不查外部)
- 预算可控 (默认 100 次/user/day)
- LLM 复审避免噪声
- 用户可关 (NUO 偏好库)
- 来源透明 (source_url + discovered_at)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
    SourceKind,
)

logger = logging.getLogger(__name__)


# 外部源 fetcher 签名
ExternalFetcher = Callable[[str], Awaitable[list[dict[str, Any]]]]
# LLM 复审签名 (raw_info → 是否对该 task_type 有用 + summary)
LLMReviewer = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]


@dataclass
class ScanBudget:
    """外部检索预算 (per-user per-day)."""

    user_id: str
    daily_limit: int = 100
    used_today: int = 0
    window_start: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ScanResult:
    """单次扫描结果摘要."""

    user_id: str
    scanned_task_types: list[str]
    sources_queried: int = 0
    candidates_added: int = 0
    candidates_rejected: int = 0
    duration_sec: float = 0.0


class ExternalInfoScanner:
    """外部信息饱和度异步守望.

    用法:
        scanner = ExternalInfoScanner(
            library=get_library(),
            fetchers={
                "github_issue": fetch_github_issues,
                "reddit": fetch_reddit,
                "arxiv": fetch_arxiv,
            },
            llm_reviewer=my_llm_reviewer,
            user_top_task_types_lookup=lambda uid: ["coding.py", "writing.email"],
            user_telemetry_enabled=lambda uid: True,
        )
        # idle-batch 周期调
        result = await scanner.scan_for_user("u-1")
    """

    def __init__(
        self,
        library: EmergentSolutionLibrary,
        *,
        fetchers: dict[SourceKind, ExternalFetcher] | None = None,
        llm_reviewer: LLMReviewer | None = None,
        user_top_task_types_lookup: Callable[[str], list[str]] | None = None,
        user_telemetry_enabled: Callable[[str], bool] | None = None,
        default_daily_limit: int = 100,
        max_candidates_per_source: int = 5,
    ) -> None:
        self._library = library
        self._fetchers = fetchers or {}
        self._llm_reviewer = llm_reviewer
        self._user_top_task_types_lookup = user_top_task_types_lookup
        self._user_telemetry_enabled = user_telemetry_enabled
        self.default_daily_limit = default_daily_limit
        self.max_candidates_per_source = max_candidates_per_source
        self._budgets: dict[str, ScanBudget] = {}

    def _get_budget(self, user_id: str) -> ScanBudget:
        b = self._budgets.get(user_id)
        if b is None:
            b = ScanBudget(user_id=user_id, daily_limit=self.default_daily_limit)
            self._budgets[user_id] = b
        # 24h 滚动
        if (datetime.now(UTC) - b.window_start).total_seconds() > 86400:
            b.used_today = 0
            b.window_start = datetime.now(UTC)
        return b

    def _under_budget(self, user_id: str, requested: int = 1) -> bool:
        b = self._get_budget(user_id)
        return (b.used_today + requested) <= b.daily_limit

    def _consume(self, user_id: str, n: int) -> None:
        b = self._get_budget(user_id)
        b.used_today += n

    async def scan_for_user(
        self,
        user_id: str,
    ) -> ScanResult:
        """异步守望周期任务: 为单个 user 扫高频 task_types.

        永不阻塞主路径 (这个函数本身就是 idle-batch 调起的).
        """
        start = datetime.now(UTC)

        # 用户禁用 telemetry → 不扫
        if self._user_telemetry_enabled is not None and not self._user_telemetry_enabled(user_id):
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[],
                duration_sec=0.0,
            )

        if self._user_top_task_types_lookup is None:
            top_types = []
        else:
            top_types = self._user_top_task_types_lookup(user_id)[:5]

        if not top_types:
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[],
                duration_sec=0.0,
            )

        sources_queried = 0
        candidates_added = 0
        candidates_rejected = 0

        for task_type in top_types:
            for source_kind, fetcher in self._fetchers.items():
                if not self._under_budget(user_id):
                    logger.info("scan budget exhausted for user %s, stopping", user_id)
                    break
                try:
                    raw_items = await fetcher(task_type)
                except Exception:
                    logger.exception("fetcher %s failed", source_kind)
                    continue
                self._consume(user_id, 1)
                sources_queried += 1

                # 限 max_candidates_per_source
                for raw in raw_items[: self.max_candidates_per_source]:
                    relevant, summary = await self._review(task_type, raw)
                    if not relevant:
                        candidates_rejected += 1
                        continue

                    sol = EmergentSolution(
                        task_type=task_type,
                        discovered_by="external_scan",
                        source=EmergentSource(
                            kind=source_kind,
                            url=raw.get("url", ""),
                            snippet=raw.get("snippet", "")[:300],
                        ),
                        description=summary,
                        estimated_outcome_delta=float(raw.get("estimated_outcome_delta", 0.0)),
                        estimated_cost_delta=float(raw.get("estimated_cost_delta", 0.0)),
                    )
                    self._library.add(sol)
                    candidates_added += 1

        elapsed = (datetime.now(UTC) - start).total_seconds()
        return ScanResult(
            user_id=user_id,
            scanned_task_types=top_types,
            sources_queried=sources_queried,
            candidates_added=candidates_added,
            candidates_rejected=candidates_rejected,
            duration_sec=elapsed,
        )

    async def scan_for_user_anchor_then_expand(
        self,
        user_id: str,
        *,
        max_rounds: int = 3,
    ) -> AsyncIterator[ScanResult]:
        """按需扫描外部来源.

        老的 ``scan_for_user`` 会遍历用户高频任务 × 所有来源. 新接口先扫最靠前的
        一个来源, 调用方需要更多信息时再继续 expand 后续来源.

        # TODO: wire by Claude in V2.2
        """
        if self._user_telemetry_enabled is not None and not self._user_telemetry_enabled(user_id):
            return

        top_types = (
            []
            if self._user_top_task_types_lookup is None
            else self._user_top_task_types_lookup(user_id)[:5]
        )
        pairs = [
            (task_type, source_kind, fetcher)
            for task_type in top_types
            for source_kind, fetcher in self._fetchers.items()
        ]
        if not pairs:
            return

        async def anchor_fn() -> ScanResult:
            task_type, source_kind, fetcher = pairs[0]
            return await self._scan_one_source(user_id, task_type, source_kind, fetcher)

        async def expand_fn(
            _anchor: ScanResult,
            prior: list[ScanResult],
        ) -> ScanResult | None:
            idx = len(prior)
            if idx >= len(pairs):
                return None
            task_type, source_kind, fetcher = pairs[idx]
            return await self._scan_one_source(user_id, task_type, source_kind, fetcher)

        async for result in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield result

    async def _scan_one_source(
        self,
        user_id: str,
        task_type: str,
        source_kind: SourceKind,
        fetcher: ExternalFetcher,
    ) -> ScanResult:
        """扫描一个 task_type/source 组合."""
        start = datetime.now(UTC)
        if not self._under_budget(user_id):
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[task_type],
                duration_sec=0.0,
            )

        try:
            raw_items = await fetcher(task_type)
        except Exception:
            logger.exception("fetcher %s failed", source_kind)
            return ScanResult(
                user_id=user_id,
                scanned_task_types=[task_type],
                duration_sec=(datetime.now(UTC) - start).total_seconds(),
            )

        self._consume(user_id, 1)
        candidates_added = 0
        candidates_rejected = 0
        for raw in raw_items[: self.max_candidates_per_source]:
            relevant, summary = await self._review(task_type, raw)
            if not relevant:
                candidates_rejected += 1
                continue

            sol = EmergentSolution(
                task_type=task_type,
                discovered_by="external_scan",
                source=EmergentSource(
                    kind=source_kind,
                    url=raw.get("url", ""),
                    snippet=raw.get("snippet", "")[:300],
                ),
                description=summary,
                estimated_outcome_delta=float(raw.get("estimated_outcome_delta", 0.0)),
                estimated_cost_delta=float(raw.get("estimated_cost_delta", 0.0)),
            )
            self._library.add(sol)
            candidates_added += 1

        return ScanResult(
            user_id=user_id,
            scanned_task_types=[task_type],
            sources_queried=1,
            candidates_added=candidates_added,
            candidates_rejected=candidates_rejected,
            duration_sec=(datetime.now(UTC) - start).total_seconds(),
        )

    async def _review(
        self,
        task_type: str,
        raw: dict[str, Any],
    ) -> tuple[bool, str]:
        """LLM 复审避免噪声. 没注册 reviewer → 默认接受."""
        if self._llm_reviewer is None:
            return (True, raw.get("snippet", "")[:200])
        try:
            return await self._llm_reviewer(task_type, raw)
        except Exception:
            logger.exception("llm_reviewer failed, accepting raw")
            return (True, raw.get("snippet", "")[:200])

    def get_budget_status(self, user_id: str) -> dict[str, Any]:
        b = self._get_budget(user_id)
        return {
            "user_id": user_id,
            "daily_limit": b.daily_limit,
            "used_today": b.used_today,
            "remaining": max(0, b.daily_limit - b.used_today),
            "window_start": b.window_start.isoformat(),
        }


__all__ = [
    "ExternalFetcher",
    "ExternalInfoScanner",
    "LLMReviewer",
    "ScanBudget",
    "ScanResult",
]
