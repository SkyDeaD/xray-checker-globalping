"""Сборщик: сопоставление «из мира», уровни «из России», живучесть цикла."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app import store
from app.collector import StatusState, ru_level, run_collector, run_ru, run_world
from app.sources import ProbeRow, RateLimitedError, SourceError, XrayProxy
from tests.conftest import loc

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class _World:
    def __init__(self, proxies=None, exc=None):
        self.proxies, self.exc = proxies, exc

    async def fetch(self):
        if self.exc:
            raise self.exc
        return self.proxies

    async def aclose(self):
        return None


class _Ru:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    async def measure(self, target, port, limit):
        self.calls.append((target, port, limit))
        result = self.behaviour(target)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self):
        return None


def _rows(ok, total):
    return [ProbeRow("Москва", f"P{i}", 1, i < ok, 20 if i < ok else None) for i in range(total)]


# --------------------------------------------------------------------------- из мира


async def test_world_matches_by_server_port_not_name(sessionmaker):
    locs = [
        loc("NL", [("nl.test", 3068)]),
        loc("PL", [("pl.test", 2053), ("pl2.test", 2053)]),
        loc("DE", [("de.test", 1)]),
        loc("FI", [("fi.test", 1)]),
    ]
    world = _World([
        XrayProxy("NL.test", 3068, True, 80),       # регистр адреса не важен
        XrayProxy("nl.test", 9999, True, 10),       # чужой порт — не наш
        XrayProxy("pl.test", 2053, False, 0),
        XrayProxy("pl2.test", 2053, True, 120),
        XrayProxy("pl2.test", 2053, True, 70),
        XrayProxy("de.test", 1, False, 0),
    ])
    await run_world(locs, world, sessionmaker, now=NOW)
    latest = await store.latest_checks(sessionmaker)
    nl = latest[("NL", "world")]
    assert (nl.level, nl.latency_ms, nl.ok_count, nl.total_count) == ("green", 80, 1, 1)
    pl = latest[("PL", "world")]
    assert (pl.level, pl.latency_ms, pl.ok_count, pl.total_count) == ("green", 70, 2, 3)
    assert latest[("DE", "world")].level == "red"
    assert latest[("FI", "world")].level == "unknown"   # xray-checker этот сервер не знает


async def test_world_source_error_is_unknown_not_red(sessionmaker):
    await run_world([loc("NL", [("nl.test", 1)])], _World(exc=SourceError("boom")), sessionmaker)
    latest = await store.latest_checks(sessionmaker)
    assert latest[("NL", "world")].level == "unknown"


async def test_world_online_without_latency_gives_green_without_latency_ms(sessionmaker):
    await run_world([loc("NL", [("nl.test", 1)])], _World([XrayProxy("nl.test", 1, True, 0)]),
                    sessionmaker, now=NOW)
    got = (await store.latest_checks(sessionmaker))[("NL", "world")]
    assert (got.level, got.latency_ms) == ("green", None)


# --------------------------------------------------------------------------- уровни и квота


@pytest.mark.parametrize(
    ("ok", "total", "level"),
    [(20, 20, "green"), (16, 20, "green"), (15, 20, "yellow"), (10, 20, "yellow"),
     (9, 20, "red"), (0, 20, "red"), (0, 0, "unknown")],
)
def test_ru_level(ok, total, level):
    assert ru_level(ok, total) == level


# --------------------------------------------------------------------------- из России


async def test_run_ru_levels_probes_and_first_target_only(sessionmaker):
    locs = [
        loc("NL", [("nl1.test", 1), ("nl2.test", 2)]),
        loc("PL", [("pl.test", 3)]),
        loc("DE", [("de.test", 4)]),
    ]
    ru = _Ru(lambda t: {"nl1.test": _rows(20, 20), "pl.test": _rows(15, 20),
                        "de.test": _rows(2, 20)}[t])
    await run_ru(locs, ru, sessionmaker, limit=20, now=NOW)
    assert [c[0] for c in ru.calls] == ["nl1.test", "pl.test", "de.test"]   # вторая цель NL не опрашивалась
    assert ru.calls[0][2] == 20                                            # limit уходит в источник
    latest = await store.latest_checks(sessionmaker)
    assert latest[("NL", "ru")].level == "green"
    assert (latest[("PL", "ru")].level, latest[("PL", "ru")].ok_count, latest[("PL", "ru")].total_count) == (
        "yellow", 15, 20)
    assert latest[("DE", "ru")].level == "red"
    checked_at, rows = await store.get_probes(sessionmaker, "PL")
    assert len(rows) == 20 and sum(r["ok"] for r in rows) == 15 and checked_at > 0
    # строки по зондам: город, провайдер, asn, результат, мс
    assert rows[0] == {"city": "Москва", "provider": "P0", "asn": 1, "ok": True, "ms": 20}


async def test_run_ru_error_is_unknown_and_keeps_old_probes(sessionmaker):
    await store.save_probes(sessionmaker, "NL", [{"city": "Старый", "provider": "P", "ok": True, "ms": 1}])
    locs = [loc("NL", [("nl.test", 1)]), loc("PL", [("pl.test", 1)])]
    ru = _Ru(lambda t: SourceError("down") if t == "nl.test" else _rows(20, 20))
    await run_ru(locs, ru, sessionmaker, limit=20)      # не падает
    latest = await store.latest_checks(sessionmaker)
    assert latest[("NL", "ru")].level == "unknown"
    assert latest[("PL", "ru")].level == "green"
    _, rows = await store.get_probes(sessionmaker, "NL")
    assert rows[0]["city"] == "Старый"                  # старый замер не затирается «нет данных»


async def test_run_ru_rate_limit_stops_round_without_crash(sessionmaker):
    locs = [loc("NL", [("nl.test", 1)]), loc("PL", [("pl.test", 1)])]
    ru = _Ru(lambda t: RateLimitedError(30))
    await run_ru(locs, ru, sessionmaker, limit=20)
    assert len(ru.calls) == 1                            # после 429 остальные не опрашиваем
    latest = await store.latest_checks(sessionmaker)
    assert latest[("NL", "ru")].level == "unknown" and latest[("PL", "ru")].level == "unknown"


async def test_run_ru_location_without_targets_is_unknown(sessionmaker):
    await run_ru([loc("NL", [])], _Ru(lambda t: _rows(20, 20)), sessionmaker, limit=20)
    assert (await store.latest_checks(sessionmaker))[("NL", "ru")].level == "unknown"


async def test_run_ru_empty_probe_list_is_unknown(sessionmaker):
    await run_ru([loc("NL", [("nl.test", 1)])], _Ru(lambda t: []), sessionmaker, limit=20)
    assert (await store.latest_checks(sessionmaker))[("NL", "ru")].level == "unknown"


# --------------------------------------------------------------------------- цикл


async def test_collector_loop_survives_failures_and_collects(sessionmaker):
    class FlakyChecker(_World):
        calls = 0

        async def fetch(self):
            FlakyChecker.calls += 1
            if FlakyChecker.calls == 1:
                raise RuntimeError("checker down")
            return [XrayProxy("nl.test", 1, True, 40, "NL", ""), XrayProxy("pl.test", 1, True, 50, "PL", "")]

    state = StatusState()
    task = asyncio.create_task(run_collector(
        sessionmaker=sessionmaker, state=state,
        world=FlakyChecker(), ru=_Ru(lambda t: _rows(20, 20)),
        world_interval=0, ru_interval=0, ru_limit=20, tick=0.01,
    ))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if state.locations and len(await store.latest_checks(sessionmaker)) >= 4:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert FlakyChecker.calls >= 2                        # первый сбой не убил цикл
    assert [loc.name for loc in state.locations] == ["NL", "PL"]
    assert state.ru_interval >= 0


async def test_collector_uses_manual_targets_without_checker(sessionmaker):
    state = StatusState()
    task = asyncio.create_task(run_collector(
        sessionmaker=sessionmaker, state=state, world=None,
        ru=_Ru(lambda t: _rows(19, 20)), manual_targets=("Moscow=ru.test:443",),
        world_interval=0, ru_interval=0, ru_limit=20, tick=0.01,
    ))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if await store.latest_checks(sessionmaker):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [loc.targets for loc in state.locations] == [[("ru.test", 443)]]
    assert (await store.latest_checks(sessionmaker))[("moscow", "ru")].level == "green"


async def test_collector_raises_cancelled_error(sessionmaker):
    state = StatusState()
    task = asyncio.create_task(run_collector(
        sessionmaker=sessionmaker, state=state, world=None, ru=None,
        world_interval=0, ru_interval=0, ru_limit=20, tick=5,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
