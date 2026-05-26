"""Supervisor Pool — 多实例按 audit 维度分流 (L4.2, ADR-020 §监督线 Pool).

之前: 单一 SupervisorService 处理所有事件 + 维护单一阈值状态.
L4.2: 多实例 Pool, 每个 instance 专责一个 audit 维度
  - latency_audit:  duration_outlier + task_anomaly
  - cost_audit:     llm_fallback_spike + context_oversized_spike
  - safety_audit:   task_failure_spike + skill_mismatch_spike
  - 兜底 catchall:  其他事件 fallback

设计:
  - 每 instance 自己 dedup_key 命名空间 (前缀含 audit_dim) — 不互扰
  - 同事件可被多 instance 消费 (e.g. task.failed 既算 safety 也算 task_anomaly)
  - 配置从 env var 或显式构造
  - Pool 暴露统一 observe API; 内部 fan-out

复用 SupervisorService 实例化 — 不重写阈值检测.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from kun.agents.supervisor.service import (
    NotificationSender,
    SearchRequestEmitter,
    SupervisorService,
)
from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.pool")


# 4 个 audit 维度 → 该维度感兴趣的 event_type
_DEFAULT_AUDIT_DIMENSIONS: dict[str, set[str]] = {
    "latency_audit": {
        "task.done",  # duration_outlier check
    },
    "cost_audit": {
        "llm.fallback.triggered",
        "llm.invoke.completed",  # context_oversized check
    },
    "safety_audit": {
        "task.failed",
        "skill.invocation.completed",
        "task.done",  # task_anomaly_score 也算 safety
    },
}
"""按 audit 维度的事件订阅. 同事件可被多维度消费 — fan-out."""


@dataclass
class SupervisorPoolConfig:
    """Pool 配置 — 维度名 → 该维度订阅的 event_types."""

    dimensions: dict[str, set[str]] = field(
        default_factory=lambda: {
            k: set(v) for k, v in _DEFAULT_AUDIT_DIMENSIONS.items()
        }
    )

    def dimensions_for_event(self, event_type: str) -> list[str]:
        """该 event_type 应路由到的维度列表 (可多 → fan-out)."""
        return [
            dim
            for dim, types in self.dimensions.items()
            if event_type in types
        ]


class SupervisorPool:
    """多 SupervisorService instance 调度器.

    入口与单 instance 同: observe(event_type, payload). 内部按
    Pool config 决定 fan-out 到哪些 instance.

    每 instance 独立状态, 独立 dedup_key 命名空间 (通过 tenant_id
    自然隔离 — 不同维度的 dedup_key 不冲突, 因为我们在 dim 维度
    用不同 SupervisorService 实例).

    通用 emitter / notification_sender 注入到所有 instance.
    """

    def __init__(
        self,
        *,
        config: SupervisorPoolConfig | None = None,
        emitter: SearchRequestEmitter | None = None,
        notification_sender: NotificationSender | None = None,
        dedup_ttl_sec: int | None = None,
    ) -> None:
        self._config = config or SupervisorPoolConfig()
        self._emitter = emitter
        self._notification_sender = notification_sender
        self._dedup_ttl_sec = dedup_ttl_sec
        self._instances: dict[str, SupervisorService] = {}
        self._lock = asyncio.Lock()

    def _make_instance(self, dim: str) -> SupervisorService:
        kwargs: dict[str, Any] = {
            "emitter": self._emitter,
            "notification_sender": self._notification_sender,
        }
        if self._dedup_ttl_sec is not None:
            kwargs["dedup_ttl_sec"] = self._dedup_ttl_sec
        svc = SupervisorService(**kwargs)
        return svc

    def instance_for(self, dim: str) -> SupervisorService:
        """获取 (lazy-init) 指定 audit 维度的 SupervisorService."""
        if dim not in self._instances:
            if dim not in self._config.dimensions:
                # 未配置维度 — 不在 config 里. 创建一个空订阅的 instance, 返回
                # (允许未来动态加 dimension)
                log.warning("supervisor_pool.unknown_dimension", dim=dim)
            self._instances[dim] = self._make_instance(dim)
        return self._instances[dim]

    async def observe(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """事件分流到对应维度. 返回 {dim: [triggered_requests]}."""
        dims = self._config.dimensions_for_event(event_type)
        results: dict[str, list[dict[str, Any]]] = {}

        if not dims:
            log.debug(
                "supervisor_pool.no_matching_dimension",
                event_type=event_type,
            )
            return results

        # 并行 fan-out (各 dim instance 互不依赖)
        async with self._lock:
            tasks = []
            for dim in dims:
                svc = self.instance_for(dim)
                tasks.append((dim, svc.observe(event_type, payload)))

        # asyncio.gather 在外面跑, 避免持锁等 LLM 调用
        gathered = await asyncio.gather(
            *(coro for _, coro in tasks), return_exceptions=True
        )
        for (dim, _), result in zip(tasks, gathered, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "supervisor_pool.dimension_observe_failed",
                    dim=dim,
                    error=str(result),
                )
                results[dim] = []
            else:
                results[dim] = result

        return results

    def all_dimensions(self) -> list[str]:
        return list(self._config.dimensions.keys())


__all__ = [
    "SupervisorPool",
    "SupervisorPoolConfig",
]
