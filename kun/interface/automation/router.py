"""Adapter Router — 选 API 还是 Browser (ADR-026 §Router).

规则 (engineering-first, 与 LLM Router capability_card 同源思路):

  1. action.requested_kind 显式指定 → 用它 (调用方有 final say)
  2. 否则 API adapter 优先 (快 + 稳 + cost low) — 如果其 capability_score ≥ threshold
  3. API capability_score 跌破 / API 不可用 → fallback Browser
  4. 完全没注册的 (platform, operation) → 返回 AdapterSelection(kind=None, reason="not registered")

capability_score 怎么算 (与 LLM Router 同源):
  - 历史 N 次 execute 成功率, sample_size >= 10 才有 weight
  - sample_size < 10 → cold-start damping (sample/30 weight)
  - cooldown: health_check 失败 → 5 分钟 cooldown 不选

不在本提交实装 capability_card 真接 — 留 hook 给 LLM Router 同源升级.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.interface.automation.base import (
        Action,
        ActionResult,
        AutomationAdapter,
        AutomationKind,
    )
    from kun.interface.automation.registry import AdapterRegistry

log = get_logger("kun.interface.automation.router")


DEFAULT_API_PREFERENCE_SCORE = 0.7
"""API capability_score >= 此值 → 优先用 API; 跌破 fallback browser."""

DEFAULT_HEALTH_COOLDOWN_SEC = 300
"""adapter health_check 失败后 cooldown 不调."""


@dataclass(frozen=True)
class AdapterSelection:
    """Router 决策结果."""

    selected: AutomationAdapter | None
    kind: AutomationKind | None  # None 表 not found
    reason: str
    fallback_candidate: AutomationAdapter | None = None
    """如果 selected 失败, 备选下一个 adapter (通常是 browser 兜底)"""


@dataclass(frozen=True)
class RouterDecision:
    """单次 route_and_execute 完整决策 + 结果 trail."""

    action_id: str
    selection: AdapterSelection
    result: ActionResult | None
    fallback_used: bool = False
    rationale: str = ""


@dataclass
class _AdapterHealth:
    """单 adapter 的健康度内存状态."""

    last_check_at: datetime | None = None
    last_check_ok: bool = True
    cooldown_until: datetime | None = None
    success_count: int = 0
    failure_count: int = 0

    @property
    def sample_size(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        if self.sample_size == 0:
            return 0.5  # neutral
        return self.success_count / self.sample_size

    def damped_score(self) -> float:
        """sample-size aware score (与 LLM Router cold-start damping 同源)."""
        if self.sample_size == 0:
            return 0.5
        weight = min(1.0, self.sample_size / 30.0)
        return 0.5 + weight * (self.success_rate - 0.5)


class AdapterRouter:
    """Router: 从 Registry 拿候选, 按 health + preference 选 adapter, execute, fallback.

    state per-adapter (in-memory, 与 LLM Router 同思路; production 接 Redis):
      - 健康度 cooldown
      - sample-size aware success rate (damping)
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        *,
        api_preference_score: float = DEFAULT_API_PREFERENCE_SCORE,
        health_cooldown_sec: int = DEFAULT_HEALTH_COOLDOWN_SEC,
    ) -> None:
        self._registry = registry
        self._api_pref_score = api_preference_score
        self._health_cooldown = timedelta(seconds=health_cooldown_sec)
        # adapter id (object id) → health state
        self._health: dict[int, _AdapterHealth] = {}

    def _health_for(self, adapter: AutomationAdapter) -> _AdapterHealth:
        key = id(adapter)
        if key not in self._health:
            self._health[key] = _AdapterHealth()
        return self._health[key]

    def _is_cooled_down(self, adapter: AutomationAdapter, now: datetime) -> bool:
        h = self._health_for(adapter)
        if h.cooldown_until is None:
            return False
        return now < h.cooldown_until

    def select(self, action: Action) -> AdapterSelection:
        """决定用哪个 adapter, 但不真 execute. Pure 函数 (除查内存 health)."""
        candidates = self._registry.candidates_for(
            platform=action.target_platform, operation=action.operation
        )
        if not candidates:
            return AdapterSelection(
                selected=None,
                kind=None,
                reason=(
                    f"no adapter registered for ({action.target_platform}, "
                    f"{action.operation})"
                ),
            )

        now = datetime.now(UTC)
        # 排掉 cooldown 中的
        usable = [c for c in candidates if not self._is_cooled_down(c, now)]
        if not usable:
            return AdapterSelection(
                selected=None,
                kind=None,
                reason="all candidates in cooldown (recent health_check failures)",
            )

        # action 显式指定 kind 优先
        if action.requested_kind is not None:
            for c in usable:
                if c.kind == action.requested_kind:
                    fallback = next(
                        (other for other in usable if other is not c), None
                    )
                    return AdapterSelection(
                        selected=c,
                        kind=c.kind,
                        reason=f"requested_kind={action.requested_kind} matched",
                        fallback_candidate=fallback,
                    )
            return AdapterSelection(
                selected=None,
                kind=None,
                reason=(
                    f"requested_kind={action.requested_kind} but no matching adapter; "
                    "use None to let router decide"
                ),
            )

        # 按 kind 分组
        api_adapters = [c for c in usable if c.kind == "api"]
        browser_adapters = [c for c in usable if c.kind == "browser"]

        # API 优先策略: 如果 API capability_score >= preference threshold, 用 API
        if api_adapters:
            best_api = max(api_adapters, key=lambda a: self._health_for(a).damped_score())
            best_score = self._health_for(best_api).damped_score()
            if best_score >= self._api_pref_score or not browser_adapters:
                fallback = browser_adapters[0] if browser_adapters else None
                return AdapterSelection(
                    selected=best_api,
                    kind="api",
                    reason=f"API preferred (score={best_score:.2f} >= {self._api_pref_score})",
                    fallback_candidate=fallback,
                )

        # API 不达标 → 走 Browser
        if browser_adapters:
            best_browser = max(
                browser_adapters, key=lambda a: self._health_for(a).damped_score()
            )
            best_score = self._health_for(best_browser).damped_score()
            return AdapterSelection(
                selected=best_browser,
                kind="browser",
                reason=f"API below threshold or unavailable; browser score={best_score:.2f}",
                fallback_candidate=None,  # browser 已是兜底
            )

        # 不应到这 — 因为 usable 不空但 api/browser 都空 (illegal kind?)
        return AdapterSelection(
            selected=None,
            kind=None,
            reason="usable candidates have unknown kind",
        )

    async def route_and_execute(self, action: Action) -> RouterDecision:
        """select → execute → 失败 fallback → 记录 health.

        正常路径:
          select → execute → ok → record_success → 返回 (fallback_used=False)

        Fallback 路径:
          select → execute → failed/timeout → 如果有 fallback_candidate, 切到它
            → execute → 记录 health (primary 算 failure, fallback 算 success/failure)
            → 返回 (fallback_used=True)
        """
        selection = self.select(action)
        if selection.selected is None:
            return RouterDecision(
                action_id=action.action_id,
                selection=selection,
                result=None,
                fallback_used=False,
                rationale=selection.reason,
            )

        try:
            result = await selection.selected.execute(action)
        except Exception as e:
            log.warning(
                "adapter_router.execute_raised",
                adapter_kind=selection.selected.kind,
                platform=selection.selected.platform,
                error=str(e),
            )
            self._record_failure(selection.selected)
            result = None

        primary_ok = result is not None and result.status == "ok"
        if primary_ok and result is not None:
            self._record_success(selection.selected)
            return RouterDecision(
                action_id=action.action_id,
                selection=selection,
                result=result,
                fallback_used=False,
                rationale=f"primary {selection.kind} succeeded",
            )

        # primary 失败 — 是否有 fallback?
        if result is not None:
            self._record_failure(selection.selected)
        if selection.fallback_candidate is None:
            return RouterDecision(
                action_id=action.action_id,
                selection=selection,
                result=result,
                fallback_used=False,
                rationale=f"primary {selection.kind} failed; no fallback configured",
            )

        # 切 fallback
        fallback = selection.fallback_candidate
        try:
            fb_result = await fallback.execute(action)
        except Exception as e:
            log.warning(
                "adapter_router.fallback_raised",
                error=str(e),
            )
            self._record_failure(fallback)
            return RouterDecision(
                action_id=action.action_id,
                selection=selection,
                result=result,
                fallback_used=True,
                rationale=f"primary failed; fallback {fallback.kind} also raised",
            )

        if fb_result.status == "ok":
            self._record_success(fallback)
        else:
            self._record_failure(fallback)

        return RouterDecision(
            action_id=action.action_id,
            selection=selection,
            result=fb_result,
            fallback_used=True,
            rationale=(
                f"primary {selection.kind} failed; fallback {fallback.kind} "
                f"status={fb_result.status}"
            ),
        )

    def _record_success(self, adapter: AutomationAdapter) -> None:
        h = self._health_for(adapter)
        h.success_count += 1
        h.last_check_at = datetime.now(UTC)
        h.last_check_ok = True

    def _record_failure(self, adapter: AutomationAdapter) -> None:
        h = self._health_for(adapter)
        h.failure_count += 1
        h.last_check_at = datetime.now(UTC)
        h.last_check_ok = False

    async def health_check_all(self) -> dict[str, Any]:
        """对所有 registered adapter 跑 health_check, 失败的进 cooldown."""
        results: dict[str, Any] = {}
        for platform in self._registry.all_platforms():
            for op in self._registry.operations_for(platform):
                adapters = self._registry.candidates_for(platform=platform, operation=op)
                for adapter in adapters:
                    try:
                        ok = await adapter.health_check()
                    except Exception as e:
                        log.warning(
                            "health_check.raised",
                            platform=platform,
                            kind=adapter.kind,
                            error=str(e),
                        )
                        ok = False
                    h = self._health_for(adapter)
                    h.last_check_at = datetime.now(UTC)
                    h.last_check_ok = ok
                    if not ok:
                        h.cooldown_until = h.last_check_at + self._health_cooldown
                    results.setdefault(platform, {})
                    results[platform].setdefault(op, []).append(
                        {"kind": adapter.kind, "healthy": ok}
                    )
        return results

    def snapshot_health(self) -> dict[str, Any]:
        """读所有 adapter 的健康度快照 (debugging / monitoring)."""
        return {
            "adapters": [
                {
                    "platform": platform,
                    "operation": op,
                    "kind": adapter.kind,
                    "success_count": self._health_for(adapter).success_count,
                    "failure_count": self._health_for(adapter).failure_count,
                    "damped_score": self._health_for(adapter).damped_score(),
                    "cooldown_until": (
                        self._health_for(adapter).cooldown_until.isoformat()
                        if self._health_for(adapter).cooldown_until
                        else None
                    ),
                }
                for platform in self._registry.all_platforms()
                for op in self._registry.operations_for(platform)
                for adapter in self._registry.candidates_for(
                    platform=platform, operation=op
                )
            ]
        }


__all__ = ["AdapterRouter", "AdapterSelection", "RouterDecision"]
