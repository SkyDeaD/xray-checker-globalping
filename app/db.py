"""Хранилище сервиса: SQLite через SQLAlchemy/aiosqlite, таблицы — идемпотентным DDL.

Миграций нет: схема своя и меняется вместе с кодом; ``CREATE TABLE IF NOT EXISTS``
при старте. База — файл на томе compose, иначе история пропадёт при пересоздании.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .store import STATUS_DDL


def make_engine(db_path: str) -> AsyncEngine:
    if db_path in ("", ":memory:"):
        url = "sqlite+aiosqlite:///:memory:"
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def bootstrap(engine: AsyncEngine) -> None:
    """Создать наши таблицы, если их нет. Повторный вызов ничего не меняет."""

    async with engine.begin() as conn:
        for ddl in STATUS_DDL:
            await conn.execute(text(ddl))
