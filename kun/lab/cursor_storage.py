"""LabRecipeAdoptionStep cursor 持久化 (Wire 29B).

Wire 23 的 AdoptionState (last_adopted_at + adopted_promotion_ids) 默认存
in-memory — 进程重启会丢, 重启后 idle_batch 重 adopt 同一批 promotion (虽
然 KP 端会被 strategy 去重, 但浪费 cycle + 把 events 重新走一遍).

Wire 29B 加 CursorStorage 抽象:
    - InMemoryCursorStorage (默认): 现行为, 单元测试用
    - SqlCursorStorage: 接 SQLAlchemy session, 自动 CREATE TABLE IF NOT EXISTS
      (lab_adoption_cursor), upsert/load. 不依赖 alembic — 自包含

LabRecipeAdoptionStep 接 storage 注入式 (向后兼容). 默认 InMemory; 真生产
install_lab_adoption_step(storage=SqlCursorStorage(session_factory)).

数据库 schema (单表):
    CREATE TABLE lab_adoption_cursor (
        cursor_name VARCHAR(64) PRIMARY KEY,
        last_adopted_at TIMESTAMPTZ NOT NULL,
        adopted_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

Wire 30 / M5 后续: 接 alembic migration (替代 self-managed CREATE TABLE) +
truncate adopted_ids 防表无限增长 (e.g. 只留最近 1000).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class CursorSnapshot:
    """Cursor 状态序列化形式."""

    last_adopted_at: datetime
    adopted_promotion_ids: list[str]

    @classmethod
    def empty(cls) -> CursorSnapshot:
        return cls(
            last_adopted_at=datetime.fromtimestamp(0, tz=UTC),
            adopted_promotion_ids=[],
        )


class CursorStorage(Protocol):
    """Cursor 存取协议. 实现要 idempotent + 异常向上传 (caller 决定 fallback)."""

    async def load(self, cursor_name: str) -> CursorSnapshot: ...
    async def save(self, cursor_name: str, snapshot: CursorSnapshot) -> None: ...


class InMemoryCursorStorage:
    """默认 storage. 单元测试 / 未配 DB 场景."""

    def __init__(self) -> None:
        self._store: dict[str, CursorSnapshot] = {}

    async def load(self, cursor_name: str) -> CursorSnapshot:
        snapshot = self._store.get(cursor_name)
        return snapshot if snapshot is not None else CursorSnapshot.empty()

    async def save(self, cursor_name: str, snapshot: CursorSnapshot) -> None:
        # 复制以防外部 mutation
        self._store[cursor_name] = CursorSnapshot(
            last_adopted_at=snapshot.last_adopted_at,
            adopted_promotion_ids=list(snapshot.adopted_promotion_ids),
        )

    def reset(self) -> None:
        self._store.clear()


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lab_adoption_cursor (
    cursor_name VARCHAR(64) PRIMARY KEY,
    last_adopted_at TIMESTAMPTZ NOT NULL,
    adopted_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


SessionFactory = Callable[[], Any]  # 返 async context manager (session_scope)


class SqlCursorStorage:
    """SQLAlchemy 后端. 自动 CREATE TABLE IF NOT EXISTS — 不依赖 alembic.

    Args:
        session_factory: callable() → async context manager. 默认拿
                         kun.core.db.session_scope (生产). 测试可注入 in-memory.
        truncate_ids_after: adopted_ids 列表上限 (防无限增长). None = 不截断.
    """

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        truncate_ids_after: int | None = 1000,
    ) -> None:
        self._session_factory = session_factory
        self._truncate_after = truncate_ids_after
        self._table_ensured = False

    async def _ensure_table(self, session: AsyncSession) -> None:
        if self._table_ensured:
            return
        from sqlalchemy import text

        await session.execute(text(_CREATE_TABLE_SQL))
        self._table_ensured = True

    async def _open_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from kun.core.db import session_scope

        return session_scope()

    async def load(self, cursor_name: str) -> CursorSnapshot:
        from sqlalchemy import text

        async with await self._open_session() as session:
            await self._ensure_table(session)
            result = await session.execute(
                text(
                    "SELECT last_adopted_at, adopted_ids "
                    "FROM lab_adoption_cursor WHERE cursor_name = :n"
                ),
                {"n": cursor_name},
            )
            row = result.first()
            if row is None:
                return CursorSnapshot.empty()
            ts, ids = row
            if isinstance(ids, str):
                ids = json.loads(ids)
            return CursorSnapshot(
                last_adopted_at=ts,
                adopted_promotion_ids=list(ids or []),
            )

    async def save(self, cursor_name: str, snapshot: CursorSnapshot) -> None:
        from sqlalchemy import text

        ids = snapshot.adopted_promotion_ids
        if self._truncate_after is not None and len(ids) > self._truncate_after:
            ids = ids[-self._truncate_after :]
        async with await self._open_session() as session:
            await self._ensure_table(session)
            await session.execute(
                text(
                    "INSERT INTO lab_adoption_cursor "
                    "(cursor_name, last_adopted_at, adopted_ids, updated_at) "
                    "VALUES (:n, :ts, CAST(:ids AS JSONB), now()) "
                    "ON CONFLICT (cursor_name) DO UPDATE SET "
                    "last_adopted_at = EXCLUDED.last_adopted_at, "
                    "adopted_ids = EXCLUDED.adopted_ids, "
                    "updated_at = now()"
                ),
                {
                    "n": cursor_name,
                    "ts": snapshot.last_adopted_at,
                    "ids": json.dumps(ids),
                },
            )


__all__ = [
    "CursorSnapshot",
    "CursorStorage",
    "InMemoryCursorStorage",
    "SessionFactory",
    "SqlCursorStorage",
]


# Awaitable stub to satisfy mypy (Protocol Awaitable need)
_ = Awaitable
