"""Prompt template versioning for Hermes and future prompt-driven modules.

Lab recipes can recommend a prompt strategy such as ``chain_of_thought`` for a
target module. This registry keeps the actual prompt text versioned and
tenant-scoped, so Hermes can consume the winning strategy without hard-coding
strategy text inside the execution protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

HERMES_PROMPT_TARGET = "hermes_prompt_template"
WILDCARD_TASK_TYPE = "*"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_template_id(task_type: str, target_module: str, strategy: str) -> str:
    """Build a stable id for one logical prompt template family."""

    raw = f"{task_type.strip().lower()}|{target_module.strip().lower()}|{strategy.strip().lower()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"pt-{digest}"


class PromptTemplate(BaseModel):
    """A versioned prompt template row.

    ``template_id`` identifies the logical family, while ``version`` identifies
    a concrete prompt text. Only the active latest version is returned by the
    runtime registry.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "u-sylvan"
    task_type: str = WILDCARD_TASK_TYPE
    target_module: str = HERMES_PROMPT_TARGET
    strategy: str
    content: str
    template_id: str = ""
    version: int = Field(default=1, ge=1)
    source: str = "default"
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("task_type", "target_module", "strategy")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("prompt template key fields must be non-empty")
        return text

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("prompt template content must be non-empty")
        return text

    def model_post_init(self, __context: Any) -> None:
        if not self.template_id:
            self.template_id = build_template_id(self.task_type, self.target_module, self.strategy)


DEFAULT_HERMES_PROMPT_TEMPLATES: dict[str, str] = {
    "chain_of_thought": (
        "[Lab-validated recipe] Think step by step. Show your reasoning briefly before the JSON."
    ),
    "diverse_perspective": (
        "[Lab-validated recipe] Take a contrarian view first. "
        "Challenge any default assumptions before deciding."
    ),
    "tier_top_low_temp": (
        "[Lab-validated recipe] Be conservative — high stakes detected. "
        "Prefer correctness over speed."
    ),
}


def default_prompt_content_for_strategy(strategy: str) -> str | None:
    return DEFAULT_HERMES_PROMPT_TEMPLATES.get(strategy)


class PromptTemplateStorage(Protocol):
    async def load_all(self, tenant_id: str) -> list[PromptTemplate]: ...
    async def save(self, entry: PromptTemplate) -> None: ...
    async def clear(self, tenant_id: str) -> None: ...


class InMemoryPromptTemplateStorage:
    """Small async storage for unit tests and no-DB fallbacks."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int], PromptTemplate] = {}

    async def load_all(self, tenant_id: str) -> list[PromptTemplate]:
        return [
            row
            for (stored_tenant, _template, _version), row in self._rows.items()
            if stored_tenant == tenant_id
        ]

    async def save(self, entry: PromptTemplate) -> None:
        if entry.active:
            for key, existing in list(self._rows.items()):
                if key[0] == entry.tenant_id and key[1] == entry.template_id:
                    self._rows[key] = existing.model_copy(update={"active": False})
        self._rows[(entry.tenant_id, entry.template_id, entry.version)] = entry

    async def clear(self, tenant_id: str) -> None:
        for key in [key for key in self._rows if key[0] == tenant_id]:
            del self._rows[key]


class SqlPromptTemplateStorage:
    """SQLAlchemy storage for ``prompt_templates``.

    The schema is managed by alembic ``0014_prompt_templates``.
    """

    def __init__(self, session_factory: Callable[..., Any] | None = None) -> None:
        self._session_factory = session_factory

    def _open_session(self, tenant_id: str) -> Any:
        if self._session_factory is not None:
            return self._session_factory(tenant_id=tenant_id)
        from kun.core.db import session_scope

        return session_scope(tenant_id=tenant_id)

    async def load_all(self, tenant_id: str) -> list[PromptTemplate]:
        from sqlalchemy import text

        async with self._open_session(tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id, template_id, version, task_type, target_module, strategy, "
                    "content, source, active, metadata, created_at, updated_at "
                    "FROM prompt_templates WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
            rows = result.all()
        return [
            PromptTemplate(
                tenant_id=row.tenant_id,
                template_id=row.template_id,
                version=int(row.version),
                task_type=row.task_type,
                target_module=row.target_module,
                strategy=row.strategy,
                content=row.content,
                source=row.source,
                active=bool(row.active),
                metadata=dict(row.metadata or {}),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def save(self, entry: PromptTemplate) -> None:
        from sqlalchemy import text

        async with self._open_session(entry.tenant_id) as session:
            if entry.active:
                await session.execute(
                    text(
                        "UPDATE prompt_templates SET active = false, updated_at = now() "
                        "WHERE tenant_id = :tenant_id AND template_id = :template_id"
                    ),
                    {"tenant_id": entry.tenant_id, "template_id": entry.template_id},
                )
            await session.execute(
                text(
                    "INSERT INTO prompt_templates "
                    "(tenant_id, template_id, version, task_type, target_module, strategy, "
                    "content, source, active, metadata, created_at, updated_at) "
                    "VALUES (:tenant_id, :template_id, :version, :task_type, :target_module, "
                    ":strategy, :content, :source, :active, CAST(:metadata AS JSONB), "
                    ":created_at, :updated_at) "
                    "ON CONFLICT (tenant_id, template_id, version) DO UPDATE SET "
                    "task_type = EXCLUDED.task_type, "
                    "target_module = EXCLUDED.target_module, "
                    "strategy = EXCLUDED.strategy, "
                    "content = EXCLUDED.content, "
                    "source = EXCLUDED.source, "
                    "active = EXCLUDED.active, "
                    "metadata = EXCLUDED.metadata, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "tenant_id": entry.tenant_id,
                    "template_id": entry.template_id,
                    "version": entry.version,
                    "task_type": entry.task_type,
                    "target_module": entry.target_module,
                    "strategy": entry.strategy,
                    "content": entry.content,
                    "source": entry.source,
                    "active": entry.active,
                    "metadata": _json_dumps(entry.metadata),
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                },
            )

    async def clear(self, tenant_id: str) -> None:
        from sqlalchemy import text

        async with self._open_session(tenant_id) as session:
            await session.execute(
                text("DELETE FROM prompt_templates WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )


class PromptTemplateRegistry:
    """Runtime prompt template registry with optional persistent storage."""

    def __init__(
        self,
        *,
        tenant_id: str = "u-sylvan",
        storage: PromptTemplateStorage | None = None,
        seed_defaults: bool = True,
    ) -> None:
        self._tenant_id = tenant_id
        self._storage = storage
        self._entries: dict[tuple[str, str, str], PromptTemplate] = {}
        if seed_defaults:
            self.seed_default_hermes_templates()

    def seed_default_hermes_templates(self) -> None:
        for strategy, content in DEFAULT_HERMES_PROMPT_TEMPLATES.items():
            entry = PromptTemplate(
                tenant_id=self._tenant_id,
                task_type=WILDCARD_TASK_TYPE,
                target_module=HERMES_PROMPT_TARGET,
                strategy=strategy,
                content=content,
                source="default",
            )
            self.upsert(entry)

    def upsert(self, entry: PromptTemplate) -> bool:
        if not entry.active:
            return False
        key = self._key(entry.task_type, entry.target_module, entry.strategy)
        current = self._entries.get(key)
        if current is None or entry.version >= current.version:
            self._entries[key] = entry
        return True

    async def aupsert(self, entry: PromptTemplate) -> bool:
        ok = self.upsert(entry)
        if ok and self._storage is not None:
            await self._storage.save(entry)
        return ok

    async def load_from_storage(self, *, tenant_id: str | None = None) -> int:
        if self._storage is None:
            return 0
        loaded = await self._storage.load_all(tenant_id or self._tenant_id)
        for entry in loaded:
            self.upsert(entry)
        return len(loaded)

    def get(self, task_type: str, target_module: str, strategy: str) -> PromptTemplate | None:
        exact = self._entries.get(self._key(task_type, target_module, strategy))
        if exact is not None:
            return exact
        return self._entries.get(self._key(WILDCARD_TASK_TYPE, target_module, strategy))

    def all(self) -> list[PromptTemplate]:
        return list(self._entries.values())

    def next_version(self, task_type: str, target_module: str, strategy: str) -> int:
        current = self._entries.get(self._key(task_type, target_module, strategy))
        return 1 if current is None else current.version + 1

    async def aclear(self, *, tenant_id: str | None = None) -> None:
        self._entries.clear()
        self.seed_default_hermes_templates()
        if self._storage is not None:
            await self._storage.clear(tenant_id or self._tenant_id)

    @staticmethod
    def _key(task_type: str, target_module: str, strategy: str) -> tuple[str, str, str]:
        return (task_type.strip().lower(), target_module.strip().lower(), strategy.strip().lower())


_registry_singleton: PromptTemplateRegistry | None = None


def get_prompt_template_registry(
    *,
    storage: PromptTemplateStorage | None = None,
    tenant_id: str = "u-sylvan",
) -> PromptTemplateRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = PromptTemplateRegistry(storage=storage, tenant_id=tenant_id)
    return _registry_singleton


def reset_prompt_template_registry() -> None:
    global _registry_singleton
    _registry_singleton = None


async def upsert_prompt_template_from_lab_recipe(
    *,
    task_type: str,
    strategy: str,
    tenant_id: str = "u-sylvan",
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
    registry: PromptTemplateRegistry | None = None,
) -> PromptTemplate | None:
    """Create a new active Hermes prompt template version from a lab recipe."""

    resolved_content = (content or "").strip() or default_prompt_content_for_strategy(strategy)
    if not resolved_content:
        logger.debug("prompt_template.skip_unknown_strategy strategy=%s", strategy)
        return None
    reg = registry or get_prompt_template_registry(tenant_id=tenant_id)
    version = reg.next_version(task_type, HERMES_PROMPT_TARGET, strategy)
    entry = PromptTemplate(
        tenant_id=tenant_id,
        task_type=task_type,
        target_module=HERMES_PROMPT_TARGET,
        strategy=strategy,
        content=resolved_content,
        version=version,
        source="kun_lab",
        metadata=metadata or {},
    )
    await reg.aupsert(entry)
    return entry


__all__ = [
    "DEFAULT_HERMES_PROMPT_TEMPLATES",
    "HERMES_PROMPT_TARGET",
    "WILDCARD_TASK_TYPE",
    "InMemoryPromptTemplateStorage",
    "PromptTemplate",
    "PromptTemplateRegistry",
    "PromptTemplateStorage",
    "SqlPromptTemplateStorage",
    "build_template_id",
    "default_prompt_content_for_strategy",
    "get_prompt_template_registry",
    "reset_prompt_template_registry",
    "upsert_prompt_template_from_lab_recipe",
]
