"""HTTP-приложение: публичные ``/api/status`` и ``/api/status/{key}/providers``.

Данные собирает фоновая задача (``app/collector.py``); эндпоинты только читают
свои таблицы. Ответ кэшируется (``CACHE_TTL``, по умолчанию 60 с): публичный
адрес не должен превращаться в усилитель нагрузки на БД и на API Globalping.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from .collector import StatusState, run_collector
from .config import Settings
from .db import bootstrap, make_engine, make_sessionmaker
from .service import build_providers, build_status, disabled_payload
from .sources import GlobalpingSource, XrayCheckerSource

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api", tags=["status"])


def _state(request: Request) -> StatusState | None:
    return getattr(request.app.state, "status_state", None)


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    state = _state(request)
    if state is None:  # источники не настроены — сборщик не запущен
        return disabled_payload()
    cached = getattr(request.app.state, "status_cache", None)
    now = time.monotonic()
    if cached is not None and now < cached[0]:
        return cached[1]
    settings: Settings = request.app.state.settings
    payload = await build_status(
        state,
        request.app.state.sessionmaker,
        expose_targets=settings.expose_targets,
    )
    request.app.state.status_cache = (now + settings.cache_ttl, payload)
    return payload


@router.get("/status/{key}/providers")
async def get_providers(key: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    if state is None or key not in {loc.key for loc in state.locations}:
        raise HTTPException(status_code=404, detail="Локация не найдена")
    return await build_providers(request.app.state.sessionmaker, key)


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, bool]:
    return {"ok": True}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Приложение. ``settings`` передаётся только тестам — иначе читается окружение."""

    conf = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=getattr(logging, conf.log_level, logging.INFO))
        engine = make_engine(conf.db_path)
        await bootstrap(engine)
        sessionmaker = make_sessionmaker(engine)

        world = ru = None
        if conf.checker_url:
            world = XrayCheckerSource(conf.checker_url, conf.checker_user, conf.checker_password)
        else:
            logger.warning("Статус: CHECKER_URL пуст — проверки «из мира» выключены")
        if conf.globalping_token:
            ru = GlobalpingSource(
                conf.globalping_token,
                country=conf.ru_country,
                tags=conf.ru_tags,
                base_url=conf.globalping_base,
            )
        else:
            logger.warning("Статус: GLOBALPING_TOKEN пуст — проверки «из России» выключены")
        if world is None and not conf.targets:
            logger.warning(
                "Статус: локации собирать не из чего — ни CHECKER_URL, ни TARGETS. "
                "Проверок не будет."
            )
        sources = [s for s in (world, ru) if s is not None]

        app.state.settings = conf
        app.state.sessionmaker = sessionmaker
        app.state.status_state = StatusState() if sources else None
        task: asyncio.Task | None = None
        if sources:
            task = asyncio.create_task(
                run_collector(
                    sessionmaker=sessionmaker,
                    state=app.state.status_state,
                    world=world,
                    ru=ru,
                    manual_targets=conf.targets,
                    world_interval=conf.world_interval,
                    ru_interval=conf.ru_interval,
                    ru_limit=conf.ru_probes,
                    tick=conf.tick,
                )
            )
        logger.info("Сервис запущен: БД=%s, источники — %s", conf.db_path, conf.sources_note)
        try:
            yield
        finally:
            # Отменяем и ждём задачу до закрытия engine: иначе останется жить
            # «Task was destroyed but it is pending».
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for src in sources:
                await src.aclose()
            await engine.dispose()

    app = FastAPI(title="xray-checker + Globalping", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
