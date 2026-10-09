"""Источники: xray-checker («из мира») и Globalping («из России»). Сети нет."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.sources import (
    EYEBALL_TAG,
    GlobalpingSource,
    RateLimitedError,
    SourceError,
    XrayCheckerSource,
    XrayProxy,
    effective_ru_interval,
    parse_probe_rows,
)


def _xray_payload(items):
    return {"success": True, "data": items}


# --------------------------------------------------------------------------- xray-checker


async def test_xray_source_path_auth_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_xray_payload([
            {"stableId": "a", "name": "x", "server": "nl.test", "port": 3068, "online": True,
             "latencyMs": 55, "groupName": "Нидерланды"},
            {"stableId": "b", "name": "y", "server": "", "port": 1, "online": True},
            {"stableId": "c", "name": "z", "server": "pl.test", "port": "bad", "online": True},
        ]))

    src = XrayCheckerSource("http://xc:2112/", "u", "p", transport=httpx.MockTransport(handler))
    proxies = await src.fetch()
    await src.aclose()
    assert seen["path"] == "/api/v1/proxies"
    assert seen["auth"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert proxies == [XrayProxy("nl.test", 3068, True, 55, "Нидерланды", "x")]


async def test_xray_source_without_auth_sends_no_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_xray_payload([]))

    src = XrayCheckerSource("http://xc", transport=httpx.MockTransport(handler))
    assert await src.fetch() == []
    await src.aclose()
    assert seen["auth"] is None


async def test_xray_source_body_without_wrapper():
    """Ответ без обёртки ``{success, data}`` — тоже список прокси."""

    payload = [{"server": "a.test", "port": 443, "online": True}]
    src = XrayCheckerSource("http://xc", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload)))
    got = await src.fetch()
    await src.aclose()
    assert [(p.server, p.port, p.online) for p in got] == [("a.test", 443, True)]


@pytest.mark.parametrize("response", [
    httpx.Response(401), httpx.Response(500), httpx.Response(200, text="не json"),
    httpx.Response(200, json={"data": "x"}),
])
async def test_xray_source_bad_responses(response):
    src = XrayCheckerSource("http://xc", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(SourceError):
        await src.fetch()


async def test_xray_source_unreachable():
    def boom(request):
        raise httpx.ConnectError("down")

    src = XrayCheckerSource("http://xc", transport=httpx.MockTransport(boom))
    with pytest.raises(SourceError):
        await src.fetch()


# --------------------------------------------------------------------------- Globalping


def _gp_results(n_ok, n_bad):
    results = []
    for i in range(n_ok):
        results.append({
            "probe": {"city": f"C{i}", "network": f"ISP{i}", "asn": 100 + i, "country": "RU"},
            "result": {"status": "finished", "stats": {"avg": 31.6, "loss": 0, "rcv": 3, "total": 3}},
        })
    for i in range(n_bad):
        results.append({
            "probe": {"city": f"B{i}", "network": "Bad", "asn": 1},
            "result": {"status": "finished", "stats": {"avg": None, "loss": 100, "rcv": 0, "total": 3}},
        })
    return results


def test_parse_probe_rows_semantics():
    results = _gp_results(1, 1) + [
        {"probe": {"city": "X"}, "result": {"status": "failed", "failureSource": "internal"}},
        {"probe": {"city": "Y"}, "result": {"status": "failed", "failureSource": "target"}},
        {"probe": {"city": "Z"}, "result": {"status": "offline"}},
        {"probe": {"city": "W"}, "result": {"status": "in-progress"}},
    ]
    rows = parse_probe_rows(results)
    assert [(r.city, r.ok, r.ms) for r in rows] == [("C0", True, 32), ("B0", False, None), ("Y", False, None)]


def test_parse_probe_rows_fallbacks_and_garbage():
    """``loss`` вместо ``rcv``, не-числовой ``avg`` и мусор в списке — не падаем."""

    rows = parse_probe_rows([
        {"probe": {"city": "A", "asn": "7"}, "result": {"status": "finished", "stats": {"loss": 50}}},
        {"probe": {"city": "B"}, "result": {"status": "finished", "stats": {"avg": "x", "rcv": 1}}},
        "мусор",
        None,
    ])
    assert [(r.city, r.asn, r.ok, r.ms) for r in rows] == [("A", None, True, None), ("B", None, True, None)]


class _Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, delay):
        self.calls.append(delay)


async def test_globalping_request_shape_and_polling():
    log: list[tuple[str, str, dict | None, str | None]] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if request.method == "POST":
            return httpx.Response(202, json={"id": "m1", "probesCount": 20})
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(200, json={"id": "m1", "status": "in-progress", "results": []})
        return httpx.Response(200, json={"id": "m1", "status": "finished", "results": _gp_results(15, 5)})

    sleeper = _Sleeper()
    src = GlobalpingSource("tok", sleep=sleeper, transport=httpx.MockTransport(handler))
    rows = await src.measure("pl.test", 2053, 20)
    await src.aclose()

    method, path, body, auth = log[0]
    assert (method, path, auth) == ("POST", "/v1/measurements", "Bearer tok")
    assert body == {
        "type": "ping", "target": "pl.test",
        "locations": [{"country": "RU", "tags": ["eyeball-network"]}], "limit": 20,
        "measurementOptions": {"protocol": "TCP", "port": 2053, "packets": 3},
    }
    assert [e[1] for e in log[1:]] == ["/v1/measurements/m1"] * 3
    assert sleeper.calls == [1.0, 1.0, 1.0]            # опрос раз в секунду
    assert sum(r.ok for r in rows) == 15 and len(rows) == 20


async def test_globalping_country_tags_and_base_url_are_configurable():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            seen["host"] = request.url.host
            return httpx.Response(202, json={"id": "m"})
        return httpx.Response(200, json={"status": "finished", "results": []})

    src = GlobalpingSource(
        "t", country="DE", tags=("datacenter-network", "eyeball-network"),
        base_url="http://gp.local/", sleep=_Sleeper(), transport=httpx.MockTransport(handler),
    )
    await src.measure("a.test", 1, 5)
    await src.aclose()
    assert seen["host"] == "gp.local"
    assert seen["body"]["locations"] == [{"country": "DE", "tags": ["datacenter-network", "eyeball-network"]}]


async def test_globalping_without_tags_sends_country_only():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(202, json={"id": "m"})
        return httpx.Response(200, json={"status": "finished", "results": []})

    src = GlobalpingSource("t", tags=(), sleep=_Sleeper(), transport=httpx.MockTransport(handler))
    await src.measure("a.test", 1, 5)
    await src.aclose()
    assert seen["body"]["locations"] == [{"country": "RU"}]


async def test_globalping_429_waits_retry_after_then_succeeds():
    state = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(202, json={"id": "m"})
        return httpx.Response(200, json={"status": "finished", "results": _gp_results(1, 0)})

    sleeper = _Sleeper()
    src = GlobalpingSource("t", sleep=sleeper, transport=httpx.MockTransport(handler))
    rows = await src.measure("a.test", 1, 1)
    await src.aclose()
    assert sleeper.calls[0] == 7.0 and len(rows) == 1


async def test_globalping_429_uses_ratelimit_reset_and_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RateLimit-Reset": "11"})

    sleeper = _Sleeper()
    src = GlobalpingSource("t", sleep=sleeper, transport=httpx.MockTransport(handler))
    with pytest.raises(RateLimitedError) as err:
        await src.measure("a.test", 1, 1)
    await src.aclose()
    assert err.value.retry_after == 11
    assert sleeper.calls == [11.0, 11.0]


async def test_globalping_429_on_poll_waits_and_keeps_polling():
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "m"})
        state["polls"] += 1
        if state["polls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"status": "finished", "results": _gp_results(1, 0)})

    sleeper = _Sleeper()
    src = GlobalpingSource("t", sleep=sleeper, transport=httpx.MockTransport(handler))
    rows = await src.measure("a.test", 1, 1)
    await src.aclose()
    assert sleeper.calls == [1.0, 3.0, 1.0] and len(rows) == 1   # пауза 429 + обычный опрос


async def test_globalping_timeout_raises_source_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "m"})
        return httpx.Response(200, json={"status": "in-progress", "results": []})

    src = GlobalpingSource("t", timeout=5, sleep=_Sleeper(), transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError):
        await src.measure("a.test", 1, 1)
    await src.aclose()


@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(202, text="не json"), httpx.Response(202, json={"nope": 1}),
])
async def test_globalping_bad_post_responses(response):
    src = GlobalpingSource("t", sleep=_Sleeper(), transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(SourceError):
        await src.measure("a.test", 1, 1)
    await src.aclose()


async def test_globalping_unreachable():
    def boom(request):
        raise httpx.ConnectError("down")

    src = GlobalpingSource("t", sleep=_Sleeper(), transport=httpx.MockTransport(boom))
    with pytest.raises(SourceError):
        await src.measure("a.test", 1, 1)
    await src.aclose()


# --------------------------------------------------------------------------- квота


def test_effective_ru_interval_grows_with_locations():
    assert effective_ru_interval(3, 20, 900) == 900   # 240 тестов/ч
    assert effective_ru_interval(5, 20, 900) == 900   # 400/ч
    assert effective_ru_interval(6, 20, 900) == 960   # 480/ч > 450 -> интервал растёт
    assert effective_ru_interval(7, 20, 900) == 1120
    assert effective_ru_interval(0, 20, 900) == 900
    assert effective_ru_interval(3, 0, 900) == 900


def test_eyeball_tag_default():
    assert EYEBALL_TAG == "eyeball-network"
