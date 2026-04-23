"""LLM Provider 抽象 + 适配器 + 路由引擎 (ADR-002).

开发期路由顺序 (ADR-002):
  default  → Opus 4.7           (via ofox proxy)
  secondary→ Codex 5.3          (via ofox proxy, 编程专项)
  cheap    → Claude Haiku 4.5   (via ofox proxy)
  fallback → MiniMax M2.7       (直连官方 API)

路由层只看"能力标签", 不绑死厂商.
"""

from kun.interface.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMRole,
    ModelTier,
    TaskProfile,
    ToolCall,
    UsageInfo,
)
from kun.interface.llm.router import LLMRouter, get_router

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "LLMRouter",
    "ModelTier",
    "TaskProfile",
    "ToolCall",
    "UsageInfo",
    "get_router",
]
