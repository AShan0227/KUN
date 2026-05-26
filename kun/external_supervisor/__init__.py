"""External Supervisor — 独立进程的监督线 (ADR-023).

与主线 (Director / Executor) 物理隔离, 用本地模型 (ollama / llama.cpp) 做:
  - Mode A · 同步监管 (Gate / 高风险决策前的二次评审, L2.5)
  - Mode B · 任务尾复盘 (task done 后的回溯检查, L2.5)
  - 自嗨/假通过检测 (每次任务必跑, L2.5)

为什么独立进程:
  - 物理隔离主线 → 主线挂了, 监督还在
  - 资源隔离 → 本地模型推理 (慢/CPU 密集) 不抢主线异步事件循环
  - 模型隔离 → 监督看见的是 frozen 输出, 不会被主线 LLM 影响

L2.4: 工厂 + service 骨架 + 独立 runner (本提交)
L2.5: Mode A / Mode B / 自嗨检测真实现
"""

from kun.external_supervisor.service import (
    ExternalSupervisorObservation,
    ExternalSupervisorService,
)

__all__ = [
    "ExternalSupervisorObservation",
    "ExternalSupervisorService",
]
