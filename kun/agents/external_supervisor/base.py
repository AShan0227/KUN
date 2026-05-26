"""External Supervisor · 独立进程 + 本地模型 + 双模式 (ADR-020 / ADR-023).

Type      : 独立进程 (multiprocessing.Process, 不能 asyncio.Task)
Line      : 监督线
Model     : 本地 (qwen2.5-32b via ollama, 零边际成本)
Input     : event stream (NATS) + Artifact
Output    : Debrief 报告 + 自嗨检测 + drift verify
闭环      : Gate 入门禁 (Mode B debrief 不过关 → Gate 拒绝准入)

3 条硬约束 (启动时 fail-close, ADR-023):
  1. 必须独立进程 (multiprocessing.Process)
  2. 必须显式配 KUN_SUPERVISOR_MODEL
  3. 必须不同 model family (不允许 silent 自监督)

双模式:
  Mode A · 同步监管 (during task)
    订阅 kun.task.*, 每 N event 或 T 秒判一次
    audit 维度: 漂移 / 自嗨 / 假通过 / 阻塞 / context 污染
    三级响应: weak (留痕) / mid (push 主线) / high (Gate pause)

  Mode B · 任务尾复盘 (end of task, ≤30s)
    输出 1 页 debrief → evidence_ledger
    Gate 准入加约束: debrief 不过关 → 拒绝准入

4 种 audit mode (按触发激活, 不全跑):
  - 产品体验审计   — Artifact 含用户可见输出
  - 工程质量审计   — 改了代码 / config / migration
  - 用户目标对齐   — Director 判 complexity ≥ medium
  - 自嗨/假通过    — 每次必跑 (RSI 不退化的命门)

实施路径:
  L2 · 独立进程化 (docker-compose 加 service)
  L2 · LocalLLMProvider 接入 (kun/interface/llm/local_provider.py)
  L2 · Mode A 同步监管 + Mode B 任务尾复盘 + 自嗨检测必跑
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.external_supervisor")


class ExternalSupervisor(Protocol):
    """独立进程 Protocol — 双模式监管 + 复盘."""

    async def watch_sync(
        self,
        event: Any,  # Event
    ) -> Any | None:  # ObservationResult | None
        """Mode A 同步监管 — 异常 → 三级响应 (weak/mid/high)."""
        ...

    async def debrief(
        self,
        artifact: Any,
        *,
        task_meta: Any,  # TaskMeta
        event_trace: list[Any],
    ) -> Any:  # DebriefReport
        """Mode B 任务尾复盘 — 输出 debrief 给 Gate 准入用."""
        ...
