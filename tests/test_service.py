"""Публичный ответ: доступность, медиана, дни, разбор по провайдерам, отсутствие утечек."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import store
from app.collector import StatusState
from app.service import (
    build_providers,
    build_status,
    disabled_payload,
    median_ms,
    overall_status,
    uptime_info,
)
from tests.conftest import loc

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _mk(spec):
    return [(src, 1_000_000 + m * 60, lvl) for src, m, lvl in spec]


def test_uptime_info_and_median():
    assert uptime_info([]) == {"pct": None, "hours": 0.0}
    # меньше часа данных — процент не показываем
    assert uptime_info(_mk([("world", 0, "green"), ("world", 30, "red")]))["pct"] is None
    # только сегодняшние данные (6 ч): считаем по проверкам, а не 0 %
    info = uptime_info(_mk([("world", m, "green") for m in range(0, 360, 5)] + [("world", 100, "red")]))
    assert info["hours"] == 5.9 and 98.0 < info["pct"] < 100.0
    rows = [{"ok": True, "ms": 30}, {"ok": True, "ms": 50}, {"ok": True, "ms": 100}, {"ok": False, "ms": None}]
    assert median_ms(rows) == 50
    assert median_ms(rows + [{"ok": True, "ms": 70}]) == 60
    assert median_ms([{"ok": False, "ms": None}]) is None
    assert median_ms([]) is None
    assert median_ms([{"ok": True, "ms": None}]) is None


def test_overall_status():
    assert overall_status([]) == "unknown"
    assert overall_status(["unknown"]) == "unknown"
    assert overall_status(["green", "unknown"]) == "ok"
    assert overall_status(["green", "yellow"]) == "degraded"
    assert overall_status(["green", "red"]) == "degraded"
    assert overall_status(["red", "red"]) == "down"


def test_disabled_payload_shape():
    assert disabled_payload() == {"updated_at": None, "overall": "unknown", "locations": []}


async def test_status_payload_world_and_ru(sessionmaker):
    state = StatusState(locations=[
        loc("NL", [("nl.test", 443)], name="Нидерланды"),
        loc("PL", [("pl.test", 8443)], name="Польша"),
    ])
    await store.add_check(sessionmaker, key="NL", source="world", level="green", latency_ms=44,
                          ok_count=1, total_count=1, now=NOW)
    await store.add_check(sessionmaker, key="NL", source="ru", level="green", ok_count=20, total_count=20, now=NOW)
    await store.save_probes(sessionmaker, "NL", [
        {"city": "Москва", "provider": "P1", "asn": 1, "ok": True, "ms": 20},
        {"city": "Казань", "provider": "P2", "asn": 2, "ok": True, "ms": 40},
        {"city": "Омск", "provider": "P3", "asn": 3, "ok": True, "ms": 90},
    ], now=NOW)

    payload = await build_status(state, sessionmaker, now=NOW)
    assert payload["overall"] == "ok"                # у PL проверок нет вовсе, NL зелёный
    by = {item["key"]: item for item in payload["locations"]}
    assert by["NL"]["name"] == "Нидерланды"
    assert by["NL"]["world"] == {
        "level": "green", "checked_at": "2026-10-09T12:00:00Z", "latency_ms": 44, "ok": 1, "total": 1,
    }
    assert by["NL"]["ru"]["level"] == "green" and by["NL"]["ru"]["median_ms"] == 40
    assert by["PL"]["world"] is None and by["PL"]["ru"] is None and by["PL"]["uptime"]["pct"] is None
    assert len(by["NL"]["days"]) == 30 and set(by["NL"]["days"][0]) == {"date", "level"}
    assert by["NL"]["days"][-1]["level"] == "green" and by["NL"]["days"][0]["level"] == "unknown"
    assert payload["updated_at"] == "2026-10-09T12:00:00Z"


async def test_status_payload_marks_whitelist_and_country(sessionmaker):
    from app.targets import Location

    state = StatusState(locations=[
        Location(key="NL", name="Нидерланды", targets=[("nl.test", 443)],
                 country_code="NL", whitelist=False),
        Location(key="NL-wl", name="Нидерланды", targets=[("wl.test", 443)],
                 country_code="NL", whitelist=True),
    ])
    payload = await build_status(state, sessionmaker, now=NOW)
    by = {item["key"]: item for item in payload["locations"]}
    assert (by["NL"]["country_code"], by["NL"]["whitelist"]) == ("NL", False)
    assert (by["NL-wl"]["country_code"], by["NL-wl"]["whitelist"]) == ("NL", True)


async def test_status_payload_hides_addresses_unless_enabled(sessionmaker):
    state = StatusState(locations=[loc("NL", [("secret-host.internal", 44321)])])
    await store.add_check(sessionmaker, key="NL", source="world", level="green", now=NOW)
    hidden = json.dumps(await build_status(state, sessionmaker, now=NOW))
    assert "secret-host.internal" not in hidden and "44321" not in hidden
    shown = json.dumps(await build_status(state, sessionmaker, expose_targets=True, now=NOW))
    assert "secret-host.internal" in shown and "44321" in shown


async def test_status_payload_without_data_is_unknown(sessionmaker):
    payload = await build_status(StatusState(locations=[loc("NL", [("nl.test", 1)])]), sessionmaker)
    (item,) = payload["locations"]
    assert item["world"] is None and item["ru"] is None
    assert payload["overall"] == "unknown" and payload["updated_at"] is None
    assert all(day["level"] == "unknown" for day in item["days"])


async def test_providers_rows_skip_garbage(sessionmaker):
    await store.save_probes(sessionmaker, "NL", [
        {"city": "Москва", "provider": "P1", "asn": 1, "ok": True, "ms": 20},
        "мусор",
        {"city": "Омск", "provider": "P2", "ok": False, "ms": None},
    ], now=NOW)
    body = await build_providers(sessionmaker, "NL")
    assert body["checked_at"] == "2026-10-09T12:00:00Z"
    assert body["rows"] == [
        {"city": "Москва", "provider": "P1", "asn": 1, "ok": True, "ms": 20},
        {"city": "Омск", "provider": "P2", "asn": None, "ok": False, "ms": None},
    ]


async def test_providers_without_measurement(sessionmaker):
    assert await build_providers(sessionmaker, "NL") == {"checked_at": None, "rows": []}


@pytest.mark.parametrize(("ok", "total", "median"), [(1, 1, 5), (0, 3, None), (2, 3, 8)])
async def test_median_uses_only_answering_probes(sessionmaker, ok, total, median):
    rows = [{"city": f"C{i}", "provider": "P", "ok": i < ok, "ms": 5 + i * 5} for i in range(total)]
    await store.save_probes(sessionmaker, "NL", rows, now=NOW)
    await store.add_check(sessionmaker, key="NL", source="ru", level="green",
                          ok_count=ok, total_count=total, now=NOW)
    payload = await build_status(StatusState(locations=[loc("NL", [("nl.test", 1)])]), sessionmaker, now=NOW)
    assert payload["locations"][0]["ru"]["median_ms"] == median


async def test_days_window_is_30_days_ending_today(sessionmaker):
    payload = await build_status(StatusState(locations=[loc("NL", [("nl.test", 1)])]), sessionmaker, now=NOW)
    days = payload["locations"][0]["days"]
    assert days[0]["date"] == "2026-09-10" and days[-1]["date"] == "2026-10-09"
    assert days[-2]["date"] == (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
