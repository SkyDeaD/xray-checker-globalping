"""Таблицы сервиса (создаются идемпотентно при старте, миграций нет).

* ``status_check`` — история проверок: по строке на локацию/источник/замер.
  ``level``: ``green|yellow|red|unknown`` (unknown = «нет данных»: источник
  упал или не нашёл локацию). Время — unix-секунды (UTC).
* ``status_ru_probe`` — последний замер РФ по зондам (JSON), одна строка на локацию.

Хранение — 31 день; чистка в сборщике.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from .uptime import day_level

RETENTION_DAYS = 31

STATUS_CHECK_DDL = """
CREATE TABLE IF NOT EXISTS status_check (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_key VARCHAR(16) NOT NULL,
    source VARCHAR(8) NOT NULL,
    checked_at INTEGER NOT NULL,
    level VARCHAR(8) NOT NULL,
    latency_ms INTEGER,
    ok_count INTEGER,
    total_count INTEGER
)
"""
STATUS_CHECK_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_status_check_loc_src_time "
    "ON status_check (location_key, source, checked_at)"
)
STATUS_CHECK_TIME_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_status_check_time ON status_check (checked_at)"
)
STATUS_RU_PROBE_DDL = """
CREATE TABLE IF NOT EXISTS status_ru_probe (
    location_key VARCHAR(16) PRIMARY KEY,
    checked_at INTEGER NOT NULL,
    rows_json TEXT NOT NULL
)
"""

STATUS_DDL = (
    STATUS_CHECK_DDL,
    STATUS_CHECK_INDEX_DDL,
    STATUS_CHECK_TIME_INDEX_DDL,
    STATUS_RU_PROBE_DDL,
)

LEVEL_RANK = {"unknown": -1, "green": 0, "yellow": 1, "red": 2}


def worst(levels) -> str:
    """Худший из уровней; unknown, если данных нет."""

    best = "unknown"
    for lvl in levels:
        if LEVEL_RANK.get(lvl, -1) > LEVEL_RANK[best]:
            best = lvl
    return best


def _ts(moment: datetime | None) -> int:
    return int((moment or datetime.now(UTC)).timestamp())


async def add_check(
    sessionmaker,
    *,
    key: str,
    source: str,
    level: str,
    latency_ms: int | None = None,
    ok_count: int | None = None,
    total_count: int | None = None,
    now: datetime | None = None,
) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO status_check (location_key, source, checked_at, level, "
                "latency_ms, ok_count, total_count) "
                "VALUES (:k, :s, :t, :l, :ms, :ok, :tot)"
            ),
            {
                "k": key, "s": source, "t": _ts(now), "l": level,
                "ms": latency_ms, "ok": ok_count, "tot": total_count,
            },
        )
        await session.commit()


async def save_probes(sessionmaker, key: str, rows: list[dict], *, now: datetime | None = None) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO status_ru_probe (location_key, checked_at, rows_json) "
                "VALUES (:k, :t, :j) ON CONFLICT(location_key) DO UPDATE SET "
                "checked_at = excluded.checked_at, rows_json = excluded.rows_json"
            ),
            {"k": key, "t": _ts(now), "j": json.dumps(rows, ensure_ascii=False)},
        )
        await session.commit()


async def get_probes(sessionmaker, key: str) -> tuple[int, list[dict]] | None:
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text("SELECT checked_at, rows_json FROM status_ru_probe WHERE location_key = :k"),
                {"k": key},
            )
        ).first()
    if row is None:
        return None
    try:
        rows = json.loads(row[1])
    except ValueError:
        rows = []
    return int(row[0]), rows if isinstance(rows, list) else []


async def cleanup(sessionmaker, *, now: datetime | None = None) -> int:
    """Удалить записи старше ``RETENTION_DAYS``; вернуть число удалённых."""

    border = _ts((now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS))
    async with sessionmaker() as session:
        res = await session.execute(
            text("DELETE FROM status_check WHERE checked_at < :b"), {"b": border}
        )
        res2 = await session.execute(
            text("DELETE FROM status_ru_probe WHERE checked_at < :b"), {"b": border}
        )
        await session.commit()
    return (res.rowcount or 0) + (res2.rowcount or 0)


@dataclass
class LatestCheck:
    level: str
    checked_at: int
    latency_ms: int | None
    ok_count: int | None
    total_count: int | None


async def latest_checks(sessionmaker) -> dict[tuple[str, str], LatestCheck]:
    """Последняя проверка по (локация, источник)."""

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.location_key, c.source, c.level, c.checked_at, c.latency_ms, "
                    "c.ok_count, c.total_count FROM status_check c JOIN ("
                    "SELECT location_key, source, MAX(id) AS mid FROM status_check "
                    "GROUP BY location_key, source) m ON m.mid = c.id"
                )
            )
        ).all()
    return {(r[0], r[1]): LatestCheck(r[2], int(r[3]), r[4], r[5], r[6]) for r in rows}


async def check_history(
    sessionmaker, *, days: int = 30, now: datetime | None = None
) -> dict[str, list[tuple[str, int, str]]]:
    """``{ключ локации: [(источник, unix-время, уровень)]}`` за последние ``days`` суток (UTC)."""

    moment = now or datetime.now(UTC)
    start = (moment - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT location_key, source, checked_at, level FROM status_check "
                    "WHERE checked_at >= :s ORDER BY checked_at"
                ),
                {"s": _ts(start)},
            )
        ).all()
    result: dict[str, list[tuple[str, int, str]]] = {}
    for key, source, ts, level in rows:
        result.setdefault(key, []).append((source, int(ts), level))
    return result


def day_levels_from(history: dict[str, list[tuple[str, int, str]]]) -> dict[str, dict[str, str]]:
    """``{ключ: {YYYY-MM-DD (UTC): уровень дня}}`` по правилам ``uptime.day_level``."""

    out: dict[str, dict[str, str]] = {}
    for key, checks in history.items():
        per_day: dict[str, list[tuple[str, int, str]]] = {}
        for c in checks:
            per_day.setdefault(datetime.fromtimestamp(c[1], UTC).strftime("%Y-%m-%d"), []).append(c)
        out[key] = {day: day_level(items) for day, items in per_day.items()}
    return out


async def day_levels(
    sessionmaker, *, days: int = 30, now: datetime | None = None
) -> dict[str, dict[str, str]]:
    return day_levels_from(await check_history(sessionmaker, days=days, now=now))
