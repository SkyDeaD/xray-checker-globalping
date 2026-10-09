"""Тестовая обвязка: SQLite в памяти и приложение БЕЗ источников.

Источники в тестах не поднимаются: ``httpx`` ходит через ``MockTransport``, а
сборщик не стартует вовсе — в сеть (в том числе в Globalping) ничего не уходит.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import create_app
from app.config import Settings
from app.db import bootstrap, make_sessionmaker
from app.targets import Location


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    # StaticPool: все сессии видят одну и ту же in-memory БД.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    await bootstrap(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return make_sessionmaker(engine)


@pytest_asyncio.fixture
async def api(sessionmaker):
    """``(app, client)``: приложение без источников, БД — общая с тестом."""

    app = create_app(Settings(checker_url="", globalping_token="", db_path=":memory:"))
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = sessionmaker
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


def loc(key: str, targets: list[tuple[str, int]], name: str | None = None) -> Location:
    """Локация для тестов: имя по умолчанию совпадает с ключом."""

    return Location(key=key, name=name or key, targets=list(targets))
