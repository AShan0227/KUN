"""输出翻译适配器层。

把 KUN 内部结构化 JSON 翻译成接收方需要的格式：给人看的自然语言、
给外部 agent 的 A2A JSON-RPC、给企业 API 的 REST body、Markdown 报表、
HTML 邮件等。
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.interface.adapters")


class OutputAdapter(Protocol):
    name: str

    async def translate(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str: ...


_REGISTRY: dict[str, OutputAdapter] = {}

DEFAULT_MAPPING: dict[str, str] = {
    "user": "human",
    "human": "human",
    "agent": "a2a",
    "a2a": "a2a",
    "company": "rest",
    "rest": "rest",
    "doc_system": "markdown",
    "markdown": "markdown",
    "email": "email",
}


def register(adapter: OutputAdapter) -> None:
    """注册一个输出适配器。重复注册会覆盖。"""
    if adapter.name in _REGISTRY:
        log.warning("output_adapter.override", adapter=adapter.name)
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str) -> OutputAdapter:
    adapter = _REGISTRY.get(name)
    if adapter is None:
        raise KeyError(f"output adapter not registered: {name}")
    return adapter


def list_adapters() -> list[str]:
    return sorted(_REGISTRY)


async def translate(
    adapter_name: str,
    *,
    payload: dict[str, Any],
    recipient_kind: str,
    context: dict[str, Any] | None = None,
) -> str:
    """用指定适配器翻译输出。"""
    adapter = get_adapter(adapter_name)
    return await adapter.translate(
        payload=payload,
        recipient_kind=recipient_kind,
        context=context,
    )


async def translate_for(
    *,
    payload: dict[str, Any],
    recipient_kind: str,
    context: dict[str, Any] | None = None,
) -> str:
    """按 recipient_kind 选择默认适配器并翻译输出。"""
    adapter_name = DEFAULT_MAPPING.get(recipient_kind, "markdown")
    return await translate(
        adapter_name,
        payload=payload,
        recipient_kind=recipient_kind,
        context=context,
    )


def autoload_builtins() -> None:
    """导入内置适配器，让它们完成注册。"""
    for module in (
        "kun.interface.adapters.human",
        "kun.interface.adapters.a2a",
        "kun.interface.adapters.rest",
        "kun.interface.adapters.markdown",
        "kun.interface.adapters.email",
    ):
        try:
            importlib.import_module(module)
        except Exception as e:
            log.warning("output_adapter.import_failed", module=module, error=str(e))


autoload_builtins()


__all__ = [
    "DEFAULT_MAPPING",
    "OutputAdapter",
    "autoload_builtins",
    "get_adapter",
    "list_adapters",
    "register",
    "translate",
    "translate_for",
]
