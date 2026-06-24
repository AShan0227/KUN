"""LocalLLMProvider — 本地 LLM 适配器 (ADR-023 External Supervisor 用).

支持 OpenAI-compatible HTTP 接口的本地推理引擎:
  - ollama   (默认): http://localhost:11434/v1
  - llama.cpp server: http://localhost:8080/v1
  - vLLM / TGI 等

为什么独立 provider:
  - 价格固定 0 (本地推理 / 自有 GPU)
  - 没远程 API key
  - tier 单独标 "local" 避免被主线路由当 fallback 调用
  - External Supervisor 用它做独立监督, 不污染主线 token 预算

不与 OpenAIProvider 复用是为了:
  - 默认 base_url + model_id 都不同
  - 不需要 ofox proxy 逻辑
  - tier="local" 让 LLMRouter 不会自动 fallback 到它
"""

from __future__ import annotations

import time
from typing import Any

from openai import AsyncOpenAI

from kun.core.logging import get_logger
from kun.core.metrics import llm_latency_seconds, llm_request_total
from kun.interface.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    UsageInfo,
)

log = get_logger("kun.llm.local")


DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL_ID = "qwen2.5:32b"
DEFAULT_API_KEY = "ollama"  # ollama OpenAI-compat 端点忽略 key 但 SDK 要传


class LocalLLMProvider(LLMProvider):
    """OpenAI-compatible 本地推理引擎适配器.

    默认指向 ollama (http://localhost:11434/v1, qwen2.5:32b). 通过构造参数
    或环境变量切换其他 endpoint (llama.cpp / vLLM / TGI).

    cost = 0 — 本地推理. tier="cheap" 让它在主线路由里可调用但不会被
    upstream 自动 promote (Strategist 后续可显式选).
    """

    name = "local"
    supports_tools = False  # 多数本地模型 OpenAI-compat tool calling 不稳, 暂关
    supports_streaming = True
    supports_cache = False

    price_input_per_mtok = 0.0
    price_output_per_mtok = 0.0
    price_cached_per_mtok = 0.0

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = DEFAULT_API_KEY,
        timeout_sec: float = 120.0,
    ) -> None:
        self.model_id = model_id
        self.tier = "cheap"  # 让主线路由可选, 但 Strategist 优先
        self._base_url = base_url
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_sec
        )

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop:
            kwargs["stop"] = request.stop

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception:
            log.warning(
                "local_llm.invoke_failed",
                base_url=self._base_url,
                model=self.model_id,
            )
            raise

        latency = (time.perf_counter() - started) * 1000
        choice = resp.choices[0]
        content = choice.message.content or ""

        u = resp.usage
        usage = UsageInfo(
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
        )

        finish_reason = "length" if choice.finish_reason == "length" else "stop"

        llm_request_total.labels(
            provider=self.name, model=self.model_id, role="invoke"
        ).inc()
        llm_latency_seconds.labels(
            provider=self.name, model=self.model_id
        ).observe(latency / 1000)

        return LLMResponse(
            content=content,
            usage=usage,
            model=self.model_id,
            provider=self.name,
            tier=self.tier,
            cost_usd_actual=0.0,
            cost_usd_equivalent=0.0,
            latency_ms=latency,
            finish_reason=finish_reason,
        )

    async def health_check(self) -> bool:
        """ping 本地推理引擎. 网络不通 / 模型未下载 → False."""
        try:
            await self.invoke(
                LLMRequest(
                    messages=[LLMMessage(role="user", content="ping")],
                    max_tokens=4,
                )
            )
            return True
        except Exception:
            return False


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "LocalLLMProvider",
]
