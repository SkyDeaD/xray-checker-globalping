"""Панель Remnawave: клиент (три запроса) и разбор её хостов в локации."""

from __future__ import annotations

import httpx
import pytest

from app.panel import PanelAuthError, PanelError, RemnawavePanel
from app.targets import PanelTargets, parse_panel_locations

WL = "squad-wl"


def _node(uuid, country, inbounds, **kw):
    return {
        "uuid": uuid, "name": f"{uuid}-name", "countryCode": country, "port": 2222,
        "isDisabled": False, "configProfile": {"activeInbounds": inbounds}, **kw,
    }


def _host(address, port, nodes, inbound, pos=1, **kw):
    return {
        "address": address, "port": port, "nodes": nodes, "isDisabled": False,
        "viewPosition": pos, "inbound": {"configProfileInboundUuid": inbound}, **kw,
    }


NODES = {"nodes": [
    _node("n-nl", "NL", [{"uuid": "i-nl", "type": "vless"}, {"uuid": "i-nl-hy", "type": "hysteria"}]),
    _node("n-nl2", "NL", [{"uuid": "i-nl2", "type": "vless"}]),
    _node("n-pl", "PL", [{"uuid": "i-pl", "type": "vless"}, {"uuid": "i-pl-wl", "type": "vless"}]),
    _node("n-zz", "ZZ", [{"uuid": "i-zz", "type": "vless"}]),
    _node("n-off", "DE", [{"uuid": "i-de", "type": "vless"}], isDisabled=True),
]}


# --------------------------------------------------------------------------- клиент


def _panel(handler, **kw) -> RemnawavePanel:
    return RemnawavePanel("http://panel.local/", "tok", transport=httpx.MockTransport(handler),
                          sleep=kw.pop("sleep", _NoSleep()), **kw)


class _NoSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, delay):
        self.calls.append(delay)


async def test_client_paths_auth_and_envelope():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["api_key"] = request.headers.get("x-api-key")
        seen["fwd"] = request.headers.get("x-forwarded-proto")
        return httpx.Response(200, json={"response": [{"address": "nl.test", "port": 443}]})

    panel = _panel(handler)
    hosts = await panel.fetch_hosts()
    await panel.aclose()
    assert hosts == [{"address": "nl.test", "port": 443}]
    assert seen == {
        "path": "/api/hosts", "auth": "Bearer tok", "api_key": "tok", "fwd": "https",
    }


async def test_client_nodes_and_squads_unwrap_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json={"response": {"nodes": [{"uuid": "n1"}]}})
        return httpx.Response(200, json={"response": {"internalSquads": [
            {"uuid": "s1", "inbounds": [{"uuid": "i1"}, {"uuid": "i2"}]},
            {"uuid": "", "inbounds": [{"uuid": "i3"}]},
            {"uuid": "s2", "inbounds": "мусор"},
        ]}})

    panel = _panel(handler)
    assert await panel.fetch_nodes() == [{"uuid": "n1"}]
    assert await panel.fetch_squad_inbounds() == {"s1": {"i1", "i2"}, "s2": set()}
    await panel.aclose()


@pytest.mark.parametrize("body", [
    [{"uuid": "n1"}],                                  # список без конверта
    {"nodes": [{"uuid": "n1"}]},                       # конверт без response
])
async def test_client_accepts_alternative_bodies(body):
    panel = _panel(lambda r: httpx.Response(200, json=body))
    assert await panel.fetch_nodes() == [{"uuid": "n1"}]
    await panel.aclose()


@pytest.mark.parametrize("status", [401, 403])
async def test_client_reports_auth_problems(status):
    panel = _panel(lambda r: httpx.Response(status))
    with pytest.raises(PanelAuthError):
        await panel.fetch_hosts()
    await panel.aclose()


async def test_client_retries_on_429_and_honours_retry_after():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"response": []})

    sleeper = _NoSleep()
    panel = _panel(handler, sleep=sleeper)
    assert await panel.fetch_hosts() == []
    await panel.aclose()
    assert state["n"] == 2 and sleeper.calls == [3.0]


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_client_retries_temporary_failures_then_gives_up(status):
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(status)

    sleeper = _NoSleep()
    panel = _panel(handler, sleep=sleeper, max_retries=2)
    with pytest.raises(PanelError):
        await panel.fetch_hosts()
    await panel.aclose()
    assert state["n"] == 3 and len(sleeper.calls) == 2    # две паузы, третья попытка — последняя


async def test_client_does_not_retry_deterministic_500():
    """500 — не временный сбой: как в нашем ядре бота, повторять его бессмысленно."""

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(500)

    sleeper = _NoSleep()
    panel = _panel(handler, sleep=sleeper, max_retries=2)
    with pytest.raises(PanelError):
        await panel.fetch_hosts()
    await panel.aclose()
    assert state["n"] == 1 and sleeper.calls == []


async def test_client_network_error_is_panel_error():
    def boom(request):
        raise httpx.ConnectError("down")

    panel = _panel(boom, max_retries=0)
    with pytest.raises(PanelError):
        await panel.fetch_hosts()
    await panel.aclose()


async def test_client_rejects_other_statuses():
    panel = _panel(lambda r: httpx.Response(404))
    with pytest.raises(PanelError) as err:
        await panel.fetch_hosts()
    await panel.aclose()
    assert err.value.status_code == 404


# --------------------------------------------------------------------------- локации


def test_panel_parses_countries_and_skips_service_hosts():
    hosts = [
        _host("nl1.test", 443, ["n-nl"], "i-nl", pos=1),
        _host("nl2.test", 443, ["n-nl2"], "i-nl2", pos=2),
        _host("pl.test", 8443, ["n-pl"], "i-pl", pos=3),
        _host("de.test", 1, ["n-off"], "i-de", pos=4),          # нода выключена
        _host("example.com", 443, [], "i-x", pos=5),            # служебный хост без нод
        _host("hy.test", 443, ["n-nl"], "i-nl-hy", pos=6),      # hysteria2 — не проверяем
        _host("zz.test", 1, ["n-zz"], "i-zz", pos=7),           # страна без названия
        _host("", 1, ["n-nl"], "i-nl", pos=8),                  # нет адреса
        _host("bad.test", "мусор", ["n-nl"], "i-nl", pos=9),    # порт не число
    ]
    locs = parse_panel_locations(hosts, NODES)
    assert [(loc.key, loc.name, loc.targets) for loc in locs] == [
        ("NL", "Нидерланды", [("nl1.test", 443), ("nl2.test", 443)]),
        ("PL", "Польша", [("pl.test", 8443)]),
    ]
    assert [loc.country_code for loc in locs] == ["NL", "PL"]
    assert [loc.whitelist for loc in locs] == [False, False]


def test_panel_marks_whitelist_host_as_separate_location():
    hosts = [
        _host("nl.test", 443, ["n-nl"], "i-nl", pos=1),
        _host("wl.test", 443, ["n-pl"], "i-pl-wl", pos=2),
        _host("pl.test", 8443, ["n-pl"], "i-pl", pos=3),
    ]
    squads = {WL: {"i-pl-wl"}}
    locs = parse_panel_locations(hosts, NODES, squads, WL)
    assert [(loc.key, loc.name, loc.whitelist) for loc in locs] == [
        ("NL", "Нидерланды", False),
        ("PL-wl", "Польша", True),
        ("PL", "Польша", False),
    ]


def test_panel_without_whitelist_squad_does_not_split():
    hosts = [_host("wl.test", 443, ["n-pl"], "i-pl-wl", pos=1)]
    locs = parse_panel_locations(hosts, NODES, {WL: {"i-pl-wl"}}, "")
    assert [(loc.key, loc.whitelist) for loc in locs] == [("PL", False)]


def test_panel_unknown_country_is_reported_and_skipped(caplog):
    locs = parse_panel_locations([_host("zz.test", 1, ["n-zz"], "i-zz")], NODES)
    assert locs == [] and "ZZ" in caplog.text


def test_panel_deduplicates_same_target():
    hosts = [
        _host("nl.test", 443, ["n-nl"], "i-nl", pos=1),
        _host("NL.test", 443, ["n-nl2"], "i-nl2", pos=2),
    ]
    locs = parse_panel_locations(hosts, NODES)
    assert [loc.targets for loc in locs] == [[("nl.test", 443), ("NL.test", 443)]]


def test_panel_accepts_nodes_as_plain_list():
    hosts = [_host("nl.test", 443, ["n-nl"], "i-nl")]
    locs = parse_panel_locations(hosts, NODES["nodes"])
    assert [loc.key for loc in locs] == ["NL"]


async def test_panel_targets_source_reads_panel_over_http():
    """Источник локаций целиком: клиент + разбор, на фейковом HTTP."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/hosts":
            return httpx.Response(200, json={"response": [
                _host("nl.test", 443, ["n-nl"], "i-nl"),
                _host("pl.test", 8443, ["n-pl"], "i-pl-wl"),
            ]})
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json={"response": NODES})
        return httpx.Response(200, json={"response": {"internalSquads": [
            {"uuid": WL, "inbounds": [{"uuid": "i-pl-wl"}]},
        ]}})

    panel = RemnawavePanel("http://panel.local", "tok", transport=httpx.MockTransport(handler))
    targets = PanelTargets(panel, WL)
    locs = await targets.fetch()
    await targets.aclose()
    assert [(loc.key, loc.whitelist, loc.targets) for loc in locs] == [
        ("NL", False, [("nl.test", 443)]),
        ("PL-wl", True, [("pl.test", 8443)]),
    ]


async def test_panel_targets_squad_not_requested_without_whitelist_uuid():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/hosts":
            return httpx.Response(200, json={"response": []})
        return httpx.Response(200, json={"response": NODES})

    panel = RemnawavePanel("http://panel.local", "tok", transport=httpx.MockTransport(handler))
    targets = PanelTargets(panel, "")
    assert await targets.fetch() == []
    await targets.aclose()
    assert paths == ["/api/hosts", "/api/nodes"]     # за squad'ами не ходили
