"""Локации: разбор TARGETS и сборка групп из подписки xray-checker."""

from __future__ import annotations

import pytest

from app.sources import SourceError, XrayProxy
from app.targets import (
    CheckerTargets,
    Location,
    ManualTargets,
    PanelTargets,
    build_targets,
    group_proxies,
    parse_target,
    parse_targets,
    slug,
    unique_key,
)


class _Checker:
    def __init__(self, proxies=None, exc=None):
        self.proxies, self.exc = proxies, exc

    async def fetch(self):
        if self.exc:
            raise self.exc
        return self.proxies

    async def aclose(self):
        return None


def test_slug_is_latin_only():
    assert slug("Нидерланды") == ""
    assert slug("Netherlands") == "netherlands"
    assert slug("  NL / Amsterdam  ") == "nl-amsterdam"
    assert len(slug("x" * 40)) == 16


@pytest.mark.parametrize(("name", "used", "expect"), [
    ("Netherlands", set(), "netherlands"),
    ("NL", {"nl"}, "nl-2"),
    ("NL", {"nl", "nl-2"}, "nl-3"),
])
def test_unique_key(name, used, expect):
    assert unique_key(name, used) == expect


def test_unique_key_for_names_without_latin_is_stable_and_hashbased():
    first = unique_key("Нидерланды", set())
    again = unique_key("Нидерланды", set())
    assert first == again and first.startswith("loc-") and len(first) == 12
    assert unique_key("Нидерланды", {first}) != first
    assert unique_key("Польша", set()) != first


@pytest.mark.parametrize(("spec", "expect"), [
    ("NL=nl.test:443", ("NL", "nl.test", 443)),
    ("Имя = host.test:8443 ", ("Имя", "host.test", 8443)),
    ("без равно", None),
    ("NL=host.test", None),
    ("NL=:443", None),
    ("=host.test:443", None),
    ("NL=host.test:0", None),
    ("NL=host.test:70000", None),
    ("NL=host.test:abc", None),
])
def test_parse_target(spec, expect):
    assert parse_target(spec) == expect


def test_parse_targets_skips_garbage(caplog):
    locs = parse_targets(["NL=nl.test:443", "мусор", "PL=pl.test:8443"])
    assert [(loc.key, loc.name, loc.targets) for loc in locs] == [
        ("nl", "NL", [("nl.test", 443)]),
        ("pl", "PL", [("pl.test", 8443)]),
    ]
    assert "не разобрана" in caplog.text


def test_group_proxies_by_group_then_name_keeping_order():
    proxies = [
        XrayProxy("nl1.test", 443, True, 50, "Нидерланды", "NL-1"),
        XrayProxy("nl2.test", 443, True, 60, "Нидерланды", "NL-2"),
        XrayProxy("pl.test", 8443, True, 70, "", "Poland"),
        XrayProxy("pl.test", 8443, True, 71, "", "Poland"),      # дубль цели не удваиваем
    ]
    locs = group_proxies(proxies)
    assert [(loc.name, loc.targets) for loc in locs] == [
        ("Нидерланды", [("nl1.test", 443), ("nl2.test", 443)]),
        ("Poland", [("pl.test", 8443)]),
    ]
    assert len({loc.key for loc in locs}) == 2               # ключи уникальны


def test_group_proxies_without_name_falls_back_to_address():
    locs = group_proxies([XrayProxy("a.test", 443, True, 0, "", "")])
    assert [(loc.key, loc.name, loc.targets) for loc in locs] == [
        ("a-test-443", "a.test:443", [("a.test", 443)]),
    ]


async def test_manual_targets_source():
    locs = await ManualTargets(["Moscow=ru.test:443", "Нидерланды=nl.test:443"]).fetch()
    assert [(loc.targets, loc.name) for loc in locs] == [
        ([("ru.test", 443)], "Moscow"),
        ([("nl.test", 443)], "Нидерланды"),
    ]
    # у кириллического имени ключ — хеш от имени, он стабилен между запусками
    assert locs[1].key.startswith("loc-") and locs[1].key == unique_key("Нидерланды", set())


async def test_checker_targets_source_uses_groups():
    checker = _Checker([XrayProxy("nl.test", 443, True, 0, "Нидерланды", "x")])
    locs = await CheckerTargets(checker).fetch()
    assert [loc.name for loc in locs] == ["Нидерланды"]
    with pytest.raises(SourceError):
        await CheckerTargets(_Checker(exc=SourceError("down"))).fetch()


def test_build_targets_priority_and_emptiness():
    checker = _Checker([XrayProxy("nl.test", 443, True, 0, "NL", "x")])
    # TARGETS важнее панели и подписки
    got = build_targets(checker=checker, panel_url="http://p", panel_token="t",
                        manual=["Москва=ru.test:443"])
    assert isinstance(got, ManualTargets)
    # панель — когда TARGETS пуст
    got = build_targets(checker=checker, panel_url="http://p/", panel_token="t")
    assert isinstance(got, PanelTargets) and got.name == "панель Remnawave"
    # подписка — когда нет ни TARGETS, ни панели
    got = build_targets(checker=checker)
    assert isinstance(got, CheckerTargets)
    # ни одного источника
    assert build_targets(checker=None) is None
    # панель без токена не считается настроенной
    assert build_targets(checker=None, panel_url="http://p") is None


async def test_panel_targets_closes_its_client():
    class _Panel:
        closed = False

        async def fetch_hosts(self):
            return []

        async def fetch_nodes(self):
            return []

        async def fetch_squad_inbounds(self):
            return {}

        async def aclose(self):
            _Panel.closed = True

    targets = PanelTargets(_Panel())
    assert await targets.fetch() == []
    await targets.aclose()
    assert _Panel.closed is True


def test_location_targets_are_not_shared_between_instances():
    a, b = Location(key="a", name="a"), Location(key="b", name="b")
    a.targets.append(("x.test", 1))
    assert b.targets == []
