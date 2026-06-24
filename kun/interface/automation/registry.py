"""Adapter Registry — 注册 (platform, operation) → adapter list.

每个 (platform, operation) 可注册多个 adapter (e.g. API 主, Browser fallback).
Router 从 Registry 拿候选 adapter list, 再按 capability_card / 健康度排序.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.interface.automation.base import AutomationAdapter

log = get_logger("kun.interface.automation.registry")


class AdapterRegistry:
    """Adapter 注册表 — per (platform, operation) 多 adapter.

    用法:
        registry = AdapterRegistry()
        registry.register(my_shopify_api_adapter)
        registry.register(my_shopify_browser_adapter)
        candidates = registry.candidates_for(platform="shopify", operation="create_product")
        # → [api_adapter, browser_adapter]  (注册顺序)
    """

    def __init__(self) -> None:
        # key = (platform, operation), value = list of adapters in insertion order
        self._index: dict[tuple[str, str], list[AutomationAdapter]] = defaultdict(list)
        # 反向索引: platform → set of operations 已注册
        self._platform_ops: dict[str, set[str]] = defaultdict(set)

    def register(self, adapter: AutomationAdapter) -> None:
        """注册一个 adapter. 同 (platform, operation) 可多次注册不同 kind."""
        platform = adapter.platform
        for op in adapter.supported_operations:
            key = (platform, op)
            if adapter in self._index[key]:
                continue  # 同 adapter instance 不重复
            self._index[key].append(adapter)
            self._platform_ops[platform].add(op)
        log.info(
            "adapter_registry.registered",
            platform=platform,
            kind=adapter.kind,
            operations=sorted(adapter.supported_operations)[:5],
        )

    def unregister(self, adapter: AutomationAdapter) -> None:
        """从 registry 摘掉 — 用于热插拔 / 测试."""
        platform = adapter.platform
        for op in list(adapter.supported_operations):
            key = (platform, op)
            if adapter in self._index[key]:
                self._index[key].remove(adapter)
                if not self._index[key]:
                    del self._index[key]
        if platform in self._platform_ops:
            still_active = {
                op
                for (p, op), adapters in self._index.items()
                if p == platform and adapters
            }
            self._platform_ops[platform] = still_active
            if not still_active:
                del self._platform_ops[platform]

    def candidates_for(
        self,
        *,
        platform: str,
        operation: str,
    ) -> list[AutomationAdapter]:
        """拿到 (platform, operation) 注册的所有 adapter, 按注册顺序."""
        return list(self._index.get((platform, operation), []))

    def has(self, *, platform: str, operation: str) -> bool:
        return bool(self._index.get((platform, operation)))

    def all_platforms(self) -> list[str]:
        return sorted(self._platform_ops.keys())

    def operations_for(self, platform: str) -> set[str]:
        return set(self._platform_ops.get(platform, ()))

    def stats(self) -> dict[str, int]:
        return {
            "platforms": len(self._platform_ops),
            "operations": sum(len(ops) for ops in self._platform_ops.values()),
            "adapters_registered": sum(
                len(adapters) for adapters in self._index.values()
            ),
        }


__all__ = ["AdapterRegistry"]
