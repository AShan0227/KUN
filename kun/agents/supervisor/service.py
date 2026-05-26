"""Supervisor service — 工程化异常阈值检测 + 写 strategy_search_requests.

ADR-020 监督线常驻 service. 工程化优先 (规则 + 阈值) , LLM 兜底.

订阅事件流, 累积异常计数, 触发阈值时:
  1. 走 RCDH (ADR-021) 排查根因层级 — L2.6 实装
  2. 写 strategy_search_request (ADR-024) 让 Strategist 异步搜索策略
  3. 升级路径分流: weak (留痕) / mid (push 主线) / strong (Gate pause)

核心异常类型 (L2.1 实装):
  - llm_fallback_spike       : llm.fallback.triggered N 次 / window
  - task_failure_spike       : task.failed N 次 / window
  - task_anomaly_spike       : task_anomaly_score >= 0.6 (复用 ADR-015 surprise)
  - duration_outlier         : 任务时长 > 平均 N x

Dedup: 同 tenant + 同 target_module + 同 anomaly_kind 在 1 小时内不重复触发
(查 strategy_search_requests 表 status='open' AND created_at > now()-1h).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.service")

# 阈值默认值. L3 实装允许 capability-card-driven 动态调整 (闭环再升级).
_DEFAULT_FALLBACK_THRESHOLD = 3  # llm fallback 连续 3 次 / 60s 窗口
_DEFAULT_FAILURE_THRESHOLD = 3  # task 连续 3 次失败
_DEFAULT_ANOMALY_THRESHOLD = 0.6  # task_anomaly_score 高水位
_DEFAULT_DURATION_OUTLIER_RATIO = 3.0  # 任务时长 > 3x 该 task_type 平均
_DEFAULT_WINDOW_SEC = 60  # 滑动窗口
_DEFAULT_CONTEXT_OVERSIZED_INPUT_TOKENS = 80_000  # 单次 LLM call input_tokens 阈值
_DEFAULT_CONTEXT_OVERSIZED_THRESHOLD = 3  # 窗口内超阈值次数 ≥ N → spike (L3.1)


@dataclass
class _EventRecord:
    """监督线 in-memory 记录 — N 秒滑动窗口."""

    event_type: str
    timestamp: datetime
    payload: dict[str, Any]


@dataclass
class SupervisorAnomalyState:
    """单 tenant 的异常累积状态. 进程级 in-memory; 真生产用 Redis."""

    tenant_id: str
    events: list[_EventRecord] = field(default_factory=list)
    recent_search_requests: dict[str, datetime] = field(default_factory=dict)
    """dedup_key → last_emitted_at"""

    fallback_threshold: int = _DEFAULT_FALLBACK_THRESHOLD
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD
    anomaly_threshold: float = _DEFAULT_ANOMALY_THRESHOLD
    duration_outlier_ratio: float = _DEFAULT_DURATION_OUTLIER_RATIO
    window_sec: int = _DEFAULT_WINDOW_SEC
    context_oversized_input_tokens: int = _DEFAULT_CONTEXT_OVERSIZED_INPUT_TOKENS
    context_oversized_threshold: int = _DEFAULT_CONTEXT_OVERSIZED_THRESHOLD

    def _purge_old(self, now: datetime) -> None:
        """滑动窗口 — 丢掉超过 window_sec 的旧事件."""
        cutoff = now - timedelta(seconds=self.window_sec)
        self.events = [e for e in self.events if e.timestamp >= cutoff]

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        self.events.append(_EventRecord(event_type=event_type, timestamp=now, payload=payload))
        self._purge_old(now)

    def count(self, event_type: str) -> int:
        """统计当前窗口内 event_type 出现次数."""
        return sum(1 for e in self.events if e.event_type == event_type)


SearchRequestEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""回调签名: payload 进去, 异步落 strategy_search_requests 表.
L2.7 给 Strategist 接 Supervisor 时, 用真正的 DB writer; 测试用 fake."""


class SupervisorService:
    """Supervisor 常驻 service. 工程化阈值检测 + 写 strategy_search_requests.

    入口: observe(event_type, payload) — 内部按规则累积 + 触发阈值时调
    emitter 写 strategy_search_request.

    Dedup: 同 tenant + 同 target_module + 同 anomaly_kind 在 dedup_ttl_sec
    内不重复触发 (默认 1h).
    """

    DEDUP_TTL_SEC = 3600

    def __init__(
        self,
        *,
        emitter: SearchRequestEmitter | None = None,
        dedup_ttl_sec: int = DEDUP_TTL_SEC,
    ) -> None:
        self._states: dict[str, SupervisorAnomalyState] = {}
        self._emitter = emitter
        self._dedup_ttl_sec = dedup_ttl_sec
        self._lock = asyncio.Lock()

    def state_for(self, tenant_id: str) -> SupervisorAnomalyState:
        if tenant_id not in self._states:
            self._states[tenant_id] = SupervisorAnomalyState(tenant_id=tenant_id)
        return self._states[tenant_id]

    async def observe(self, event_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """处理一个事件. 检查阈值, 触发 → 写 strategy_search_request.

        Returns: 本次 observe 触发的 strategy_search_request payload 列表 (空表示无触发).
        """
        tenant_id = str(payload.get("tenant_id") or "default")
        async with self._lock:
            state = self.state_for(tenant_id)
            state.record(event_type, payload)

            triggered: list[dict[str, Any]] = []

            # 检查 4 种异常类型
            if event_type == "llm.fallback.triggered":
                req = self._check_fallback_spike(state, payload)
                if req:
                    triggered.append(req)

            elif event_type == "task.failed":
                req = self._check_failure_spike(state, payload)
                if req:
                    triggered.append(req)

            elif event_type == "task.done":
                req = self._check_anomaly_score(state, payload)
                if req:
                    triggered.append(req)
                req2 = self._check_duration_outlier(state, payload)
                if req2:
                    triggered.append(req2)

            elif event_type == "llm.invoke.completed":
                req = self._check_context_oversized(state, payload)
                if req:
                    triggered.append(req)

            # 异步写 (emitter 兜底)
            for req in triggered:
                if self._emitter is not None:
                    try:
                        await self._emitter(req)
                    except Exception as e:
                        log.warning(
                            "supervisor.search_request_emit_failed",
                            error=str(e),
                            request=req,
                        )
                else:
                    log.info("supervisor.search_request_pending_emitter", request=req)

            return triggered

    def _check_fallback_spike(
        self,
        state: SupervisorAnomalyState,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        count = state.count("llm.fallback.triggered")
        if count < state.fallback_threshold:
            return None
        target_module = payload.get("primary_provider", "llm.router") or "llm.router"
        return self._build_request(
            state=state,
            anomaly_kind="llm_fallback_spike",
            target_module=str(target_module),
            evidence=[
                {
                    "type": "fallback_count",
                    "count": count,
                    "window_sec": state.window_sec,
                    "primary_provider": payload.get("primary_provider"),
                    "primary_model": payload.get("primary_model"),
                    "reason": payload.get("reason"),
                }
            ],
            priority="medium",
        )

    def _check_failure_spike(
        self,
        state: SupervisorAnomalyState,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        count = state.count("task.failed")
        if count < state.failure_threshold:
            return None
        task_type = payload.get("task_type", "unknown")
        return self._build_request(
            state=state,
            anomaly_kind="task_failure_spike",
            target_module=f"executor.{task_type}",
            evidence=[
                {
                    "type": "failure_count",
                    "count": count,
                    "window_sec": state.window_sec,
                    "task_type": task_type,
                }
            ],
            priority="high",
        )

    def _check_anomaly_score(
        self,
        state: SupervisorAnomalyState,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        score = float(payload.get("task_anomaly_score") or payload.get("surprise_score") or 0.0)
        if score < state.anomaly_threshold:
            return None
        task_type = payload.get("task_type", "unknown")
        return self._build_request(
            state=state,
            anomaly_kind="task_anomaly_spike",
            target_module=f"executor.{task_type}",
            evidence=[
                {
                    "type": "anomaly_score",
                    "score": score,
                    "threshold": state.anomaly_threshold,
                    "task_type": task_type,
                    "task_id": payload.get("task_id"),
                }
            ],
            priority="medium",
        )

    def _check_duration_outlier(
        self,
        state: SupervisorAnomalyState,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        duration = float(payload.get("duration_sec") or 0.0)
        avg = float(payload.get("avg_duration_sec_for_task_type") or 0.0)
        if avg <= 0 or duration <= 0:
            return None
        ratio = duration / avg
        if ratio < state.duration_outlier_ratio:
            return None
        task_type = payload.get("task_type", "unknown")
        return self._build_request(
            state=state,
            anomaly_kind="duration_outlier",
            target_module=f"executor.{task_type}",
            evidence=[
                {
                    "type": "duration_outlier",
                    "duration_sec": duration,
                    "avg_duration_sec": avg,
                    "ratio": round(ratio, 2),
                    "task_type": task_type,
                }
            ],
            priority="low",
        )

    def _check_context_oversized(
        self,
        state: SupervisorAnomalyState,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """L3.1 第二条 RSI 实例: context 压缩策略.

        窗口内 input_tokens > threshold 的事件数 ≥ context_oversized_threshold
        → spike. target_module 落 llm.context (context 压缩属于 context 子系统).
        """
        input_tokens = int(payload.get("input_tokens") or 0)
        if input_tokens < state.context_oversized_input_tokens:
            return None
        # 数窗口内同样超大的事件
        oversized_count = sum(
            1
            for e in state.events
            if e.event_type == "llm.invoke.completed"
            and int(e.payload.get("input_tokens") or 0)
            >= state.context_oversized_input_tokens
        )
        if oversized_count < state.context_oversized_threshold:
            return None
        return self._build_request(
            state=state,
            anomaly_kind="context_oversized_spike",
            target_module="llm.context",
            evidence=[
                {
                    "type": "input_tokens_spike",
                    "oversized_count": oversized_count,
                    "threshold_tokens": state.context_oversized_input_tokens,
                    "window_sec": state.window_sec,
                    "latest_input_tokens": input_tokens,
                    "latest_provider": payload.get("provider"),
                    "latest_task_type": payload.get("task_type"),
                }
            ],
            priority="medium",
        )

    def _build_request(
        self,
        *,
        state: SupervisorAnomalyState,
        anomaly_kind: str,
        target_module: str,
        evidence: list[dict[str, Any]],
        priority: str,
    ) -> dict[str, Any] | None:
        """构造 strategy_search_request payload, 处理 dedup."""
        dedup_key = f"{state.tenant_id}:{target_module}:{anomaly_kind}"
        now = datetime.now(UTC)
        last_at = state.recent_search_requests.get(dedup_key)
        if last_at is not None and (now - last_at).total_seconds() < self._dedup_ttl_sec:
            log.debug(
                "supervisor.dedup_skip",
                dedup_key=dedup_key,
                cooldown_remaining=self._dedup_ttl_sec - (now - last_at).total_seconds(),
            )
            return None
        state.recent_search_requests[dedup_key] = now

        return {
            "request_id": new_id("strategy_search"),
            "tenant_id": state.tenant_id,
            "triggered_by": "anomaly_threshold",
            "target_module": target_module,
            "evidence": evidence,
            "priority": priority,
            "dedup_key": dedup_key,
            "status": "open",
            "created_at": now,
            "anomaly_kind": anomaly_kind,
        }


__all__ = [
    "SupervisorAnomalyState",
    "SupervisorService",
]
