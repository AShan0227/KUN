"""External Supervisor runner — 独立进程入口 (ADR-023).

启动方式:
  python -m kun.external_supervisor

环境变量 (kun.core.config.Settings):
  KUN_EXTERNAL_SUPERVISOR_ENABLED=true
  KUN_EXTERNAL_SUPERVISOR_MODEL_ID=qwen2.5:32b
  KUN_EXTERNAL_SUPERVISOR_BASE_URL=http://localhost:11434/v1

L2.4: 占位 runner — 启动 service + 健康检查 + 等 SIGTERM
L2.5: 接入 NATS 订阅 + Mode A/B 路由
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from kun.core.config import settings
from kun.core.logging import get_logger
from kun.external_supervisor.service import ExternalSupervisorService
from kun.interface.llm.local_provider import LocalLLMProvider

log = get_logger("kun.external_supervisor.runner")


async def build_service() -> ExternalSupervisorService:
    """从 settings 构造 service. 单独函数便于测试."""
    cfg = settings()
    provider = LocalLLMProvider(
        model_id=cfg.external_supervisor_model_id,
        base_url=cfg.external_supervisor_base_url,
        api_key=cfg.external_supervisor_api_key,
        timeout_sec=cfg.external_supervisor_timeout_sec,
    )
    return ExternalSupervisorService(
        llm_provider=provider,
        max_concurrent=cfg.external_supervisor_max_concurrent,
    )


async def _run_until_signal(service: ExternalSupervisorService) -> None:
    """主循环占位 — L2.4 只是 stub.

    L2.5 实装: 订阅 NATS `kun.external_supervisor.requests` → 路由到
    analyze_observation → 写 evidence_ledger.

    目前: 健康检查一次 + 等待 SIGTERM. 已注入 service 保留 reference
    避免被 GC 提前回收.
    """
    log.info("external_supervisor.runner.started", model=service._llm.model_id)

    # 健康检查 (一次性, 启动时探活)
    ok = await service._llm.health_check()
    if ok:
        log.info("external_supervisor.runner.health_ok")
    else:
        log.warning("external_supervisor.runner.health_failed")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _shutdown(signame: str) -> None:
        log.info("external_supervisor.runner.shutdown", signal=signame)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows / non-main-thread: SIGTERM unsupported — silently skip
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _shutdown, sig.name)

    await stop_event.wait()
    log.info("external_supervisor.runner.exited")


async def main(*, _service: ExternalSupervisorService | None = None) -> dict[str, Any]:
    """Async main. 测试可注入 service / 跳过 _run_until_signal."""
    cfg = settings()
    if not cfg.external_supervisor_enabled:
        log.warning("external_supervisor.runner.disabled_in_config")
        return {"started": False, "reason": "KUN_EXTERNAL_SUPERVISOR_ENABLED is false"}

    service = _service or await build_service()
    return {"started": True, "service": service}


if __name__ == "__main__":  # pragma: no cover

    async def _entry() -> None:
        cfg = settings()
        if not cfg.external_supervisor_enabled:
            log.error(
                "external_supervisor.runner.disabled",
                hint="set KUN_EXTERNAL_SUPERVISOR_ENABLED=true",
            )
            return
        service = await build_service()
        await _run_until_signal(service)

    asyncio.run(_entry())
