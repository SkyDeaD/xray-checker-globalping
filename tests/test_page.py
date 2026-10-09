"""Готовая страница ``/``: серверный HTML без JS — проверяем содержимое строками."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from app import store
from app.api import create_app
from app.config import Settings
from app.page import render_page
from app.service import disabled_payload
from tests.conftest import seed_stand

PAYLOAD = {
    "updated_at": "2026-10-09T12:00:00Z",
    "overall": "degraded",
    "locations": [
        {
            "key": "nl",
            "name": "Нидерланды",
            "world": {"level": "green", "checked_at": "2026-10-09T12:00:00Z",
                      "latency_ms": 44, "ok": 1, "total": 1},
            "ru": {"level": "green", "ok": 20, "total": 20, "median_ms": 37,
                   "checked_at": "2026-10-09T12:00:00Z"},
            "uptime": {"pct": 99.2, "hours": 61.5},
            "days": [{"date": f"2026-10-{d:02d}", "level": "green"} for d in range(1, 31)],
        },
        {
            "key": "pl",
            "name": "Польша",
            "world": None,
            "ru": {"level": "unknown", "ok": None, "total": None, "median_ms": None,
                   "checked_at": None},
            "uptime": {"pct": None, "hours": 0.0},
            "days": [{"date": f"2026-10-{d:02d}", "level": "unknown"} for d in range(1, 31)],
        },
    ],
}

PROVIDERS = {
    "pl": [
        {"city": "Москва", "provider": "P1", "asn": 1, "ok": True, "ms": 25},
        {"city": "Омск", "provider": "P2", "asn": None, "ok": False, "ms": None},
    ]
}


def test_page_renders_locations_levels_and_counts():
    html = render_page(PAYLOAD, PROVIDERS)
    assert html.startswith("<!doctype html>") and 'lang="ru"' in html
    assert "Нидерланды" in html and "Польша" in html
    assert "есть проблемы" in html and "Обновлено: 2026-10-09T12:00:00Z" in html
    assert "20 из 20" in html and "медиана 37 мс" in html       # из России
    assert "задержка 44 мс" in html                            # из мира, холодная
    assert "99.2 % за 61.5 ч" in html
    assert 'class="badge unknown">нет данных' in html           # у PL данных нет


def test_page_has_30_day_bars_per_location():
    html = render_page(PAYLOAD, {})
    assert html.count('<span class="bar ') == 60                # 2 локации × 30 дней
    assert 'class="bar green" title="2026-10-01: доступен"' in html
    assert 'class="bar unknown" title="2026-10-01: нет данных"' in html


def test_page_marks_whitelist_location():
    payload = {
        "updated_at": "2026-10-09T12:00:00Z",
        "overall": "ok",
        "locations": [
            {"key": "NL", "name": "Нидерланды", "country_code": "NL", "whitelist": False,
             "world": None, "ru": None, "uptime": {"pct": None, "hours": 0}, "days": []},
            {"key": "NL-wl", "name": "Нидерланды", "country_code": "NL", "whitelist": True,
             "world": None, "ru": None, "uptime": {"pct": None, "hours": 0}, "days": []},
        ],
    }
    html = render_page(payload, {})
    assert html.count("белые списки") == 1
    assert '<span class="mark">белые списки</span>' in html


def test_bar_rule_keeps_explicit_height():
    """Без явной высоты полосы аптайма схлопываются в линию (нашли на живом рендере)."""

    html = render_page(PAYLOAD, {})
    rule = next(line for line in html.splitlines() if line.startswith(".bar {"))
    assert "height:" in rule and "width:" in rule
    assert ".bars" in html and "display: inline-flex" in html


def test_empty_day_is_outlined_not_filled():
    """«Нет данных» — пустая клетка в рамке: заливка читалась как сплошная полоса."""

    html = render_page(PAYLOAD, {})
    rule = next(line for line in html.splitlines() if line.startswith(".bar.unknown {"))
    assert "transparent" in rule and "inset" in rule and "var(--empty)" in rule
    # токен должен быть определён в обеих темах, иначе рамка станет прозрачной
    assert html.count("--empty:") == 2


def test_page_explains_the_bar():
    html = render_page(PAYLOAD, {})
    assert "<b>Полоса доступности</b>" in html
    assert "слева самый старый день, справа сегодня" in html
    assert "Пустая клетка — нет данных" in html


def test_table_is_scrollable_on_narrow_screens():
    html = render_page(PAYLOAD, {})
    assert '<div class="scroll"><table>' in html
    rule = next(line for line in html.splitlines() if line.startswith(".scroll {"))
    assert "overflow-x: auto" in rule


def test_page_lists_providers_without_js():
    html = render_page(PAYLOAD, PROVIDERS)
    assert "<details>" in html and "По провайдерам в России: 1 из 2 доступны" in html
    assert "Москва" in html and "P1" in html and "25 мс" in html
    assert "Омск" in html and ">нет</span>" in html             # неудачный зонд — красный
    assert "<script" not in html.lower() and "onclick" not in html.lower()


def test_page_explains_what_is_checked():
    html = render_page(PAYLOAD, {})
    # честная оговорка про DPI и протокол — на самой странице, а не только в README
    assert "блокировку по DPI такая проверка не видит" in html
    assert "«Нет данных» означает" in html


def test_page_escapes_names_and_providers():
    payload = {
        "updated_at": None,
        "overall": "unknown",
        "locations": [{"key": "x", "name": '<img src=x onerror="alert(1)">',
                       "world": None, "ru": None, "uptime": {"pct": None, "hours": 0},
                       "days": []}],
    }
    providers = {"x": [{"city": "<b>Город</b>", "provider": "&Co", "asn": 1,
                        "ok": True, "ms": 3}]}
    html = render_page(payload, providers)
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "<b>Город</b>" not in html and "&lt;b&gt;Город&lt;/b&gt;" in html
    assert "&amp;Co" in html


def test_page_empty_state_before_first_checks():
    html = render_page(disabled_payload(), {})
    assert "Проверок пока нет" in html
    assert "Обновлено: ещё не было" in html
    assert "<table>" not in html


async def test_root_serves_html_page(api, sessionmaker):
    app, client = api
    await seed_stand(app, sessionmaker)
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Статус серверов" in body and "Нидерланды" in body and "Польша" in body
    assert "Москва" in body and "15 из 20" in body
    assert "nl.test" not in body and "44321" not in body        # адресов на странице нет


async def test_root_without_sources_shows_empty_state(api):
    _, client = api
    resp = await client.get("/")
    assert resp.status_code == 200 and "Проверок пока нет" in resp.text


async def test_root_page_never_shows_addresses_and_caches(sessionmaker):
    app = create_app(Settings(checker_url="", globalping_token="", db_path=":memory:",
                              expose_targets=True))
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = sessionmaker
        await seed_stand(app, sessionmaker)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get("/")).text
            # EXPOSE_TARGETS касается только JSON: страница адреса не показывает никогда
            assert "nl.test" not in body and "pl.test" not in body
            await store.add_check(sessionmaker, key="nl", source="world", level="red",
                                  now=datetime(2026, 10, 9, 13, tzinfo=UTC))
            assert (await client.get("/")).text == body          # кэш 60 с
