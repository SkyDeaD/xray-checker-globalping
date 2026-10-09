"""HTTP: /api/status, /api/status/{key}/providers, /healthz, кэш ответа."""

from __future__ import annotations

from datetime import UTC, datetime

from app import store
from app.collector import StatusState
from tests.conftest import loc

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


async def test_healthz(api):
    _, client = api
    resp = await client.get("/api/healthz")
    assert resp.status_code == 200 and resp.json() == {"ok": True}


async def test_status_without_sources_is_disabled(api):
    _, client = api
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    assert resp.json() == {"updated_at": None, "overall": "unknown", "locations": []}
    assert (await client.get("/api/status/NL/providers")).status_code == 404


async def _seed(app, sessionmaker):
    state = StatusState(locations=[
        loc("nl", [("nl.test", 44321)], name="Нидерланды"),
        loc("pl", [("pl.test", 84430)], name="Польша"),
    ])
    await store.add_check(sessionmaker, key="nl", source="world", level="green", now=NOW)
    await store.add_check(sessionmaker, key="pl", source="world", level="green", now=NOW)
    await store.add_check(sessionmaker, key="pl", source="ru", level="yellow",
                          ok_count=15, total_count=20, now=NOW)
    await store.save_probes(sessionmaker, "pl", [
        {"city": "Москва", "provider": "P1", "asn": 1, "ok": True, "ms": 25},
        {"city": "Омск", "provider": "P2", "asn": 2, "ok": False, "ms": None},
    ], now=NOW)
    app.state.status_state = state
    return state


async def test_status_payload_and_no_leaks(api, sessionmaker):
    app, client = api
    await _seed(app, sessionmaker)
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"] == "degraded"                      # PL «из России» жёлтый
    assert [item["key"] for item in body["locations"]] == ["nl", "pl"]
    by = {item["key"]: item for item in body["locations"]}
    assert by["pl"]["ru"]["ok"] == 15 and by["pl"]["ru"]["total"] == 20
    assert by["nl"]["world"]["level"] == "green"
    assert body["updated_at"].endswith("Z")
    assert set(by["nl"]["days"][0]) == {"date", "level"} and len(by["nl"]["days"]) == 30

    # ни адресов, ни портов, ни целевых имён в сериализованном ответе
    dump = resp.text + (await client.get("/api/status/pl/providers")).text
    assert "nl.test" not in dump and "pl.test" not in dump
    assert "44321" not in dump and "84430" not in dump


async def test_status_cache_ttl(api, sessionmaker):
    app, client = api
    await _seed(app, sessionmaker)
    first = (await client.get("/api/status")).json()
    await store.add_check(sessionmaker, key="nl", source="world", level="red", now=NOW)
    assert (await client.get("/api/status")).json() == first   # из кэша
    app.state.status_cache = None
    fresh = (await client.get("/api/status")).json()
    assert fresh["locations"][0]["world"]["level"] == "red"


async def test_providers_endpoint(api, sessionmaker):
    app, client = api
    await _seed(app, sessionmaker)
    resp = await client.get("/api/status/pl/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert [row["city"] for row in body["rows"]] == ["Москва", "Омск"]
    assert set(body["rows"][0]) == {"city", "provider", "asn", "ok", "ms"}
    assert body["checked_at"].endswith("Z")
    assert (await client.get("/api/status/xx/providers")).status_code == 404
    assert (await client.get("/api/status/nl/providers")).json()["rows"] == []


async def test_status_expose_targets_flag(sessionmaker):
    """Флаг EXPOSE_TARGETS включает адреса в ответе — проверяем на уровне приложения."""

    from httpx import ASGITransport, AsyncClient

    from app.api import create_app
    from app.config import Settings

    app = create_app(Settings(checker_url="", globalping_token="", db_path=":memory:",
                              expose_targets=True))
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = sessionmaker
        await _seed(app, sessionmaker)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get("/api/status")).json()
    by = {item["key"]: item for item in body["locations"]}
    assert by["nl"]["targets"] == [{"host": "nl.test", "port": 44321}]
