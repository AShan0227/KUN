"""Small registry for compiler backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kun.compiler.material import LightweightMaterialCompiler
from kun.compiler.models import CanonicalMaterial


class MaterialCompiler(Protocol):
    async def compile_text(
        self,
        text: str,
        *,
        tenant_id: str,
        source_uri: str = "inline:text",
        declared_kind: str | None = None,
    ) -> CanonicalMaterial: ...


@dataclass
class CompilerRegistry:
    """Register optional compiler backends without making them required."""

    default_name: str = "lightweight"
    _compilers: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default_name not in self._compilers:
            self._compilers[self.default_name] = LightweightMaterialCompiler()

    def register(self, name: str, compiler: object) -> None:
        self._compilers[name] = compiler

    def get(self, name: str | None = None) -> object:
        selected = name or self.default_name
        try:
            return self._compilers[selected]
        except KeyError as exc:
            raise KeyError(f"compiler backend not registered: {selected}") from exc

    def names(self) -> list[str]:
        return sorted(self._compilers)


default_registry = CompilerRegistry()


__all__ = ["CompilerRegistry", "MaterialCompiler", "default_registry"]
