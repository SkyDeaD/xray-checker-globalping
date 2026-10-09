"""Локации: разбор TARGETS и сборка групп из подписки xray-checker."""

from __future__ import annotations

import pytest

from app.sources import SourceError, XrayProxy
from app.targets import (
    Location,
    fetch_locations,
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


async def test_fetch_locations_manual_targets_win_over_checker():
    checker = _Checker([XrayProxy("from-checker.test", 1, True, 0)])
    locs = await fetch_locations(checker, ("Москва=ru.test:443",))
    assert [loc.targets for loc in locs] == [[("ru.test", 443)]]


async def test_fetch_locations_from_checker_and_without_any():
    checker = _Checker([XrayProxy("nl.test", 443, True, 0, "NL", "x")])
    assert [loc.name for loc in await fetch_locations(checker, ())] == ["NL"]
    assert await fetch_locations(None, ()) == []


async def test_fetch_locations_propagates_source_error():
    with pytest.raises(SourceError):
        await fetch_locations(_Checker(exc=SourceError("down")), ())


def test_location_targets_are_not_shared_between_instances():
    a, b = Location(key="a", name="a"), Location(key="b", name="b")
    a.targets.append(("x.test", 1))
    assert b.targets == []
