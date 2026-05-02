"""LLMRouterEnsembleAdapter — 让 KUN-Lab EnsembleExecutor 接 V2.2 心脏 LLMRouter (Wire 20).

EnsembleExecutor 原本接受任意 async callable invoker(prompt, path) → (text, cost, latency).
之前测试用 mock invoker. Wire 20: 把 KUN 主仓库的 LLMRouter 包成符合该接口的 adapter,
让 lab 真能端到端跑起来.

调用流程:
    router = get_router()                            # 主仓库 LLMRouter (单例)
    adapter = LLMRouterEnsembleAdapter(router)
    executor = EnsembleExecutor(adapter)
    result = await executor.run(prompt, EnsembleConfig(n_paths=5))

每条路径:
    - path.tier → 拿 router.providers[tier] (绕过 router.decide(), ensemble 要精确 tier)
    - path.temperature → LLMRequest.temperature
    - path.system_prompt_override → 注入第一条 system message
    - provider.invoke(request) → 拿 cost_usd_equivalent + latency_ms

设计原则:
- 绕过 router.decide()/降级逻辑 — ensemble 故意要 N 个不同 tier 的对照, 不能让 router
  自动把它们都收敛到 top
- 但仍走 provider.invoke() (有 retry + 异常向上传)
- tier 不在 router.providers → raise (ensemble path 失败由 EnsembleExecutor._run_one_path 收)
- 单独 lab 预算 (KUN_LAB_MODE 必须 =1, EnsembleExecutor 在 .run() 入口已校验)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kun.interface.llm.base import LLMMessage, LLMRequest, TaskProfile

if TYPE_CHECKING:
    from kun.interface.llm.router import LLMRouter
    from kun.lab.ensemble_executor import PathConfig

logger = logging.getLogger(__name__)


class LLMRouterEnsembleAdapter:
    """把 LLMRouter 包成 EnsembleExecutor 期望的 invoker callable.

    Args:
        router: KUN 主仓库 LLMRouter 实例 (一般 from kun.interface.llm.router import get_router)
        max_tokens: 单条路径上限 (默认 2048, 跟 LLMRequest 默认对齐)
        task_type: 可选, 写到 TaskProfile.task_type. 走 LLM provider 端的 trace / billing.
    """

    def __init__(
        self,
        router: LLMRouter,
        *,
        max_tokens: int = 2048,
        task_type: str = "kun_lab.ensemble",
    ) -> None:
        self._router = router
        self._max_tokens = max_tokens
        self._task_type = task_type

    async def __call__(
        self,
        prompt: str,
        path: PathConfig,
    ) -> tuple[str, float, float]:
        """跑单条 path. 返 (output_text, cost_usd, latency_sec).

        EnsembleExecutor._run_one_path 会调这个签名. 我们:
        1. 直接拿 router.providers[path.tier] (绕过 decide)
        2. 构造 LLMRequest (含 system_prompt_override + temperature)
        3. provider.invoke() → 拆 (content, cost_usd_equivalent, latency_ms/1000)
        """
        # PathConfig.tier 是宽松 str (允许实验时手填新 tier 名), 这里 cast 给
        # LLMRouter.providers 的 ModelTier Literal — 不在 dict 里就走 None 分支
        provider = self._router.providers.get(path.tier)  # type: ignore[call-overload]
        if provider is None:
            available = list(self._router.providers.keys())
            raise RuntimeError(
                f"LLMRouter has no provider for tier={path.tier!r}; available={available}"
            )

        messages: list[LLMMessage] = []
        if path.system_prompt_override:
            messages.append(LLMMessage(role="system", content=path.system_prompt_override))
        messages.append(LLMMessage(role="user", content=prompt))

        request = LLMRequest(
            messages=messages,
            temperature=path.temperature,
            max_tokens=self._max_tokens,
            profile=TaskProfile(task_type=self._task_type),
        )

        response = await provider.invoke(request)
        cost_usd = float(response.cost_usd_equivalent or response.cost_usd_actual or 0.0)
        latency_sec = float(response.latency_ms or 0.0) / 1000.0
        return response.content, cost_usd, latency_sec


def make_default_adapter(
    *,
    max_tokens: int = 2048,
    task_type: str = "kun_lab.ensemble",
) -> LLMRouterEnsembleAdapter:
    """便捷 factory: 用主仓库默认 LLMRouter (get_router()) 建 adapter."""
    from kun.interface.llm.router import get_router

    return LLMRouterEnsembleAdapter(
        get_router(),
        max_tokens=max_tokens,
        task_type=task_type,
    )


__all__ = [
    "LLMRouterEnsembleAdapter",
    "make_default_adapter",
]
