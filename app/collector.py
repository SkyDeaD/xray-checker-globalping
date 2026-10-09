"""Фоновый сборщик: локации, xray-checker («из мира») и Globalping («из России»).

Цикл НИКОГДА не падает — любая ошибка источника уходит в лог, а у локации
появляется «нет данных» (``unknown``), а не «недоступна». Отмена
(``CancelledError``) пробрасывается.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import store
from .sources import (
    ProbeRow,
    RateLimitedError,
    RuSource,
    WorldSource,
    effective_ru_interval,
)
from .targets import Location, TargetSource

logger = logging.getLogger(__name__)


def ru_level(ok: int, total: int) -> str:
    """Зелёный ≥80 %, жёлтый ≥50 %, красный ниже; нет зондов — unknown."""

    if total <= 0:
        return "unknown"
    if ok * 100 >= 80 * total:
        return "green"
    if ok * 100 >= 50 * total:
        return "yellow"
    return "red"


@dataclass
class StatusState:
    """Общее состояние: последние известные локации (для API) и фактический интервал РФ."""

    locations: list[Location] = field(default_factory=list)
    ru_interval: int = 0


async def run_world(
    locations: list[Location], source: WorldSource, sessionmaker, *, now: datetime | None = None
) -> None:
    """Один проход «из мира». Сопоставление — по (server, port)."""

    try:
        proxies = await source.fetch()
    except Exception as exc:
        logger.warning("Статус: xray-checker недоступен (%s)", exc)
        proxies = None

    index: dict[tuple[str, int], list] = {}
    for p in proxies or []:
        index.setdefault((p.server.lower(), p.port), []).append(p)

    for loc in locations:
        matched = []
        if proxies is not None:
            for address, port in loc.targets:
                matched.extend(index.get((address.lower(), port), []))
        if not matched:
            await store.add_check(sessionmaker, key=loc.key, source="world", level="unknown", now=now)
            continue
        online = [p for p in matched if p.online]
        latencies = [p.latency_ms for p in online if p.latency_ms > 0]
        await store.add_check(
            sessionmaker,
            key=loc.key,
            source="world",
            level="green" if online else "red",
            latency_ms=min(latencies) if latencies else None,
            ok_count=len(online),
            total_count=len(matched),
            now=now,
        )


async def run_ru(
    locations: list[Location],
    source: RuSource,
    sessionmaker,
    *,
    limit: int,
    now: datetime | None = None,
) -> None:
    """Один проход «из России». Проверяется ПЕРВАЯ цель локации (остальные — по квоте).

    Нужны проверки каждой цели по отдельности — заводите по локации на цель
    (``TARGETS="имя=host:port,..."``), а не группу серверов в одной локации.
    """

    rate_limited = False
    for loc in locations:
        rows: list[ProbeRow] | None = None
        if not rate_limited and loc.targets:
            address, port = loc.targets[0]
            try:
                rows = await source.measure(address, port, limit)
            except RateLimitedError as exc:
                logger.warning("Статус: Globalping 429, остальные локации пропущены (%s)", exc)
                rate_limited = True
            except Exception as exc:
                logger.warning("Статус: Globalping, локация %s: %s", loc.key, exc)
        if not rows:
            await store.add_check(sessionmaker, key=loc.key, source="ru", level="unknown", now=now)
            continue
        ok = sum(1 for r in rows if r.ok)
        await store.add_check(
            sessionmaker,
            key=loc.key,
            source="ru",
            level=ru_level(ok, len(rows)),
            ok_count=ok,
            total_count=len(rows),
            now=now,
        )
        await store.save_probes(
            sessionmaker,
            loc.key,
            [
                {"city": r.city, "provider": r.provider, "asn": r.asn, "ok": r.ok, "ms": r.ms}
                for r in rows
            ],
            now=now,
        )


async def run_collector(
    *,
    sessionmaker,
    state: StatusState,
    targets: TargetSource | None,
    world: WorldSource | None,
    ru: RuSource | None,
    world_interval: int,
    ru_interval: int,
    ru_limit: int,
    tick: float = 15.0,
) -> None:
    """Главный цикл. ``world``/``ru`` = ``None`` — источник проверок выключен."""

    next_world = next_ru = next_cleanup = 0.0
    while True:
        now = time.monotonic()
        try:
            due = (world is not None and now >= next_world) or (ru is not None and now >= next_ru)
            if due:
                # Список локаций обновляем перед проверками: сбой источника целей не
                # обнуляет страницу — работаем с последними известными.
                if targets is not None:
                    try:
                        state.locations = await targets.fetch()
                    except Exception as exc:
                        logger.warning("Статус: локации недоступны (%s: %s)", type(exc).__name__, exc)
                locs: list[Location] = state.locations

                if world is not None and now >= next_world:
                    next_world = now + world_interval
                    if locs:
                        await run_world(locs, world, sessionmaker)
                if ru is not None and now >= next_ru:
                    state.ru_interval = effective_ru_interval(len(locs), ru_limit, ru_interval)
                    if state.ru_interval != ru_interval:
                        logger.warning(
                            "Статус: интервал замеров РФ увеличен %d → %d с (бюджет квоты Globalping)",
                            ru_interval, state.ru_interval,
                        )
                    next_ru = now + state.ru_interval
                    if locs:
                        await run_ru(locs, ru, sessionmaker, limit=ru_limit)
            if now >= next_cleanup:
                next_cleanup = now + 3600
                removed = await store.cleanup(sessionmaker, now=datetime.now(UTC))
                if removed:
                    logger.info("Статус: удалено старых записей: %d", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Статус: сборщик — необработанная ошибка (цикл продолжается)")
        await asyncio.sleep(tick)
