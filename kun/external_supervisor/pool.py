"""External Supervisor Pool — 按 audit mode 分实例 (L4.3, ADR-023 §Pool).

之前 (L2.4-L2.5): 单 ExternalSupervisorService 跑所有 mode (gate_review /
task_debrief / self_aggrandizement). L4 让 mode 实例化:

  - 每 mode 独立 ExternalSupervisorService instance
  - 每 instance 可有独立 LLM provider (e.g. gate_review 用快模型,
    task_debrief 可用更深推理)
  - 每 instance 独立 concurrency budget (asyncio.Semaphore)
  - Pool dispatch by mode

设计原则:
  1. 复用 ExternalSupervisorService 不重写 (与 Supervisor Pool 同思路)
  2. Pool 注入共享或独立 LLM provider 由调用方决定
  3. Mode 配置可缩 (e.g. 仅 gate_review) 或扩 (加 experimental mode)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kun.core.logging import get_logger
from kun.external_supervisor.service import (
    ExternalSupervisorObservation,
    ExternalSupervisorService,
)
from kun.interface.llm.base import LLMProvider

log = get_logger("kun.external_supervisor.pool")


@dataclass
class ExternalSupervisorPoolEntry:
    """Pool 单实例配置."""

    mode: str
    max_concurrent: int = 2
    temperature: float = 0.2
    max_tokens: int = 512


_DEFAULT_MODES: dict[str, ExternalSupervisorPoolEntry] = {
    "gate_review": ExternalSupervisorPoolEntry(
        mode="gate_review",
        max_concurrent=2,
        temperature=0.1,  # gate 决策更确定
        max_tokens=512,
    ),
    "task_debrief": ExternalSupervisorPoolEntry(
        mode="task_debrief",
        max_concurrent=1,  # debrief 可串行, 不占 budget
        temperature=0.3,  # 复盘允许更发散
        max_tokens=1024,  # debrief 输出更长
    ),
    "self_aggrandizement": ExternalSupervisorPoolEntry(
        mode="self_aggrandizement",
        max_concurrent=3,  # 高频, 多并发
        temperature=0.1,
        max_tokens=256,  # 简短 verdict
    ),
}


@dataclass
class ExternalSupervisorPoolConfig:
    """Pool 配置 — mode → entry."""

    entries: dict[str, ExternalSupervisorPoolEntry] = field(
        default_factory=lambda: dict(_DEFAULT_MODES)
    )

    def has_mode(self, mode: str) -> bool:
        return mode in self.entries

    def all_modes(self) -> list[str]:
        return list(self.entries.keys())


class ExternalSupervisorPool:
    """多 ExternalSupervisorService instance 调度器.

    每 mode 一个 instance, lazy-init. 调用方 dispatch(mode, ...) 路由.
    所有 instance 共享同一 LLM provider (默认), 或可在 build 时
    传 provider_factory 让每 mode 独立 provider.

    与 Supervisor Pool 同思路: 配置 + lazy-init + 共享 / 独立 注入.
    """

    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        config: ExternalSupervisorPoolConfig | None = None,
        provider_factory: object | None = None,
    ) -> None:
        """provider_factory: 若注入则每 mode 调用一次 factory(mode) 拿 provider;
        否则全 mode 共享同一 llm_provider."""
        self._llm_provider = llm_provider
        self._config = config or ExternalSupervisorPoolConfig()
        self._provider_factory = provider_factory
        self._instances: dict[str, ExternalSupervisorService] = {}

    def _provider_for(self, mode: str) -> LLMProvider:
        if self._provider_factory is not None:
            try:
                return self._provider_factory(mode)  # type: ignore[operator]
            except Exception as e:
                log.warning(
                    "external_supervisor_pool.factory_failed",
                    error=str(e),
                    mode=mode,
                )
        return self._llm_provider

    def instance_for(self, mode: str) -> ExternalSupervisorService:
        if mode not in self._instances:
            entry = self._config.entries.get(mode)
            if entry is None:
                # 未配置 mode — 用默认 budget 创建一个 (容错优先)
                log.warning(
                    "external_supervisor_pool.unknown_mode_using_default",
                    mode=mode,
                )
                entry = ExternalSupervisorPoolEntry(mode=mode)
            self._instances[mode] = ExternalSupervisorService(
                llm_provider=self._provider_for(mode),
                max_concurrent=entry.max_concurrent,
                temperature=entry.temperature,
                max_tokens=entry.max_tokens,
            )
        return self._instances[mode]

    async def dispatch(
        self,
        mode: str,
        *,
        obs_kind: str,
        observation_payload: dict[str, Any],
        anchor: dict[str, Any] | None = None,
        target_task_id: str | None = None,
        target_anchor_id: str | None = None,
    ) -> ExternalSupervisorObservation:
        """根据 mode 选 instance → 调 analyze_observation."""
        svc = self.instance_for(mode)
        return await svc.analyze_observation(
            obs_kind=obs_kind,
            observation_payload=observation_payload,
            anchor=anchor,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
        )

    def all_modes(self) -> list[str]:
        return self._config.all_modes()


__all__ = [
    "ExternalSupervisorPool",
    "ExternalSupervisorPoolConfig",
    "ExternalSupervisorPoolEntry",
]
