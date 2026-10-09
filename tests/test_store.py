"""Хранение: история проверок, уровни дня, доступность, чистка."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app import store
from app.collector import StatusState
from app.service import build_status
from app.uptime import DAY_OUTAGE_MINUTES, availability_pct, day_level, longest_outage_minutes
from tests.conftest import loc

UTC_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _mk(spec):
    """[(источник, минуты от начала, уровень)] -> проверки."""

    return [(src, 1_000_000 + m * 60, lvl) for src, m, lvl in spec]


def test_day_level_by_share_not_any_failure():
    assert day_level([]) == "unknown"
    assert day_level(_mk([("world", 0, "unknown")])) == "unknown"
    # 1 сбой из 288 (0,35 %) — день зелёный
    green = [("world", i * 5, "green") for i in range(287)] + [("world", 2000, "red")]
    assert day_level(_mk(green)) == "green"
    # 10 сбоев из 100 разрозненно = 90 % — не ниже порога, но сбои есть: жёлтый
    mixed = [("world", i * 5, "red" if i % 10 == 0 else "green") for i in range(100)]
    assert day_level(_mk(mixed)) == "yellow"
    # 12 из 100 — ниже 90 %: красный
    bad = [("world", i * 5, "red" if i % 8 == 0 else "green") for i in range(100)]
    assert day_level(_mk(bad)) == "red"
    # только частичные (yellow) проверки РФ: жёлтый, не красный
    assert day_level(_mk([("ru", i * 15, "yellow") for i in range(10)])) == "yellow"
    # unknown не считается
    assert day_level(_mk([("world", 0, "red"), ("world", 5, "unknown")])) == "red"


def test_day_level_long_outage_is_red_even_with_high_share():
    # 13 красных подряд (60 мин) среди ~300 зелёных: 4 % сбоев, но простой >= 60 мин
    spec = [("world", i * 5, "green") for i in range(100)]
    spec += [("world", 500 + i * 5, "red") for i in range(DAY_OUTAGE_MINUTES // 5 + 1)]
    spec += [("world", 700 + i * 5, "green") for i in range(200)]
    assert day_level(_mk(spec)) == "red"
    # тот же объём красных, но зелёная проверка посередине рвёт серию — день жёлтый
    spec2 = [("world", 500 + i * 5, "red") for i in range(6)] + [("world", 530, "green")]
    spec2 += [("world", 535 + i * 5, "red") for i in range(6)]
    spec2 += [("world", 1000 + i * 5, "green") for i in range(300)]
    assert day_level(_mk(spec2)) == "yellow"


def test_longest_outage_ignores_unknown_and_other_sources():
    checks = _mk([("world", 0, "red"), ("world", 10, "unknown"), ("world", 20, "red"),
                  ("ru", 0, "green"), ("ru", 100, "green")])
    assert longest_outage_minutes(checks) == 20


def test_availability_pct():
    assert availability_pct([]) is None
    assert availability_pct(_mk([("world", 0, "unknown")])) is None
    got = availability_pct(_mk([("world", 0, "green"), ("ru", 0, "yellow"),
                                ("world", 5, "red"), ("world", 9, "unknown")]))
    assert got == pytest.approx(200 / 3)


def test_umbrella_worst_picks_worst_known_level():
    assert store.worst([]) == "unknown"
    assert store.worst(["green", "unknown"]) == "green"
    assert store.worst(["green", "yellow", "red"]) == "red"


async def test_latest_check_is_the_newest_per_source(sessionmaker):
    await store.add_check(sessionmaker, key="NL", source="ru", level="red", now=UTC_NOW - timedelta(minutes=5))
    await store.add_check(sessionmaker, key="NL", source="ru", level="green", ok_count=20, total_count=20, now=UTC_NOW)
    await store.add_check(sessionmaker, key="NL", source="world", level="green", latency_ms=44, now=UTC_NOW)
    latest = await store.latest_checks(sessionmaker)
    assert latest[("NL", "ru")].level == "green" and latest[("NL", "ru")].ok_count == 20
    assert latest[("NL", "world")].latency_ms == 44


async def test_probes_roundtrip_and_replace(sessionmaker):
    assert await store.get_probes(sessionmaker, "NL") is None
    await store.save_probes(sessionmaker, "NL", [{"city": "A", "ok": True}], now=UTC_NOW)
    await store.save_probes(sessionmaker, "NL", [{"city": "B", "ok": False}], now=UTC_NOW)
    checked_at, rows = await store.get_probes(sessionmaker, "NL")
    assert [r["city"] for r in rows] == ["B"] and checked_at == int(UTC_NOW.timestamp())


async def test_day_levels_from_db_only_today(sessionmaker):
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    # утром сбои настройки (короткие), потом стабильно зелёно
    for i in range(6):
        await store.add_check(sessionmaker, key="NL", source="world", level="red",
                              now=now - timedelta(minutes=300 - i * 5))
    for i in range(60):
        await store.add_check(sessionmaker, key="NL", source="world", level="green",
                              now=now - timedelta(minutes=240 - i * 4))
    await store.add_check(sessionmaker, key="NL", source="world", level="green",
                          now=now - timedelta(days=45))
    days = (await store.day_levels(sessionmaker, now=now))["NL"]
    assert days == {"2026-10-06": "yellow"}               # 6/66 = 9 % сбоев, простой 25 мин
    payload = await build_status(
        StatusState(locations=[loc("NL", [("nl.test", 1)])]), sessionmaker, now=now)
    (item,) = payload["locations"]
    assert item["uptime"]["pct"] == 90.9 and item["uptime"]["hours"] > 4
    assert item["days"][-1]["level"] == "yellow" and item["days"][0]["level"] == "unknown"


async def test_cleanup_removes_older_than_31_days(sessionmaker):
    await store.add_check(sessionmaker, key="NL", source="world", level="green",
                          now=UTC_NOW - timedelta(days=32))
    await store.add_check(sessionmaker, key="NL", source="world", level="green",
                          now=UTC_NOW - timedelta(days=30))
    await store.save_probes(sessionmaker, "OLD", [], now=UTC_NOW - timedelta(days=40))
    await store.save_probes(sessionmaker, "NEW", [], now=UTC_NOW - timedelta(days=1))
    assert await store.cleanup(sessionmaker, now=UTC_NOW) == 2
    async with sessionmaker() as session:
        left = (await session.execute(text("SELECT COUNT(*) FROM status_check"))).scalar()
    assert left == 1
    assert await store.get_probes(sessionmaker, "OLD") is None
    assert await store.get_probes(sessionmaker, "NEW") is not None
