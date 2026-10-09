"""Локации: что именно проверяем и как это называется в публичном API.

Локация — группа целей ``(host, port)`` с одним именем. Способов собрать две:

* ``TARGETS="Нидерланды=nl.example.com:443,Польша=pl.example.com:8443"`` — вручную,
  по одной цели на локацию. Годится, когда xray-checker не используется.
* из подписки xray-checker: берутся пары ``server:port`` каждого прокси, имя
  локации — ``groupName`` прокси (группа-балансировщик подписки), а если группа
  пустая — имя прокси. Несколько серверов в одной группе = одна локация.

Ключ локации — читаемый латинский слаг имени (``Amsterdam`` → ``amsterdam``); для
имён без латиницы (``Нидерланды``) ключ — стабильный ``loc-`` + 8 hex от имени,
чтобы он не зависел от порядка целей. Ключ попадает в URL ``/api/status/{key}/providers``.

Адреса и порты целей живут только в памяти сборщика и в публичный API не попадают,
пока не включён ``EXPOSE_TARGETS``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from .sources import WorldSource, XrayProxy

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
#: Максимальная длина ключа (по DDL: VARCHAR(16)).
KEY_MAX = 16


@dataclass
class Location:
    key: str
    name: str
    #: (host, port) — НАРУЖУ НЕ ОТДАЁТСЯ, пока не включён EXPOSE_TARGETS.
    targets: list[tuple[str, int]] = field(default_factory=list)


def slug(name: str) -> str:
    """Латинский слаг из имени; ``""``, если латиницы не осталось (кириллица и пр.)."""

    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")[:KEY_MAX].strip("-")


def unique_key(name: str, used: set[str]) -> str:
    """Ключ, не занятый ранее: латинский слаг или ``loc-<8 hex от имени>``.

    Хеш от имени, а не от позиции: ключ не должен меняться, когда в подписке
    переставили серверы. При совпадении — суффикс ``-2``, ``-3``.
    """

    base = slug(name) or "loc-" + hashlib.sha1(name.strip().encode()).hexdigest()[:8]
    key = base
    n = 2
    while key in used:  # ключ повторяется — различаем номером
        suffix = f"-{n}"
        key = base[: KEY_MAX - len(suffix)] + suffix
        n += 1
    used.add(key)
    return key


def parse_target(spec: str) -> tuple[str, str, int] | None:
    """``Имя=host:port`` → ``(имя, host, port)``; мусор — ``None`` (в лог, не падаем)."""

    if "=" not in spec:
        return None
    name, _, address = spec.partition("=")
    name = name.strip()
    host, sep, raw_port = address.strip().rpartition(":")
    if not name or not sep or not host:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    return name, host, port


def parse_targets(specs: tuple[str, ...] | list[str]) -> list[Location]:
    """Список локаций из ``TARGETS``. Некорректные строки пропускаются с предупреждением."""

    used: set[str] = set()
    out: list[Location] = []
    for spec in specs:
        parsed = parse_target(spec)
        if parsed is None:
            logger.warning("TARGETS: строка %r не разобрана (нужно «Имя=host:port»)", spec)
            continue
        name, host, port = parsed
        out.append(Location(key=unique_key(name, used), name=name, targets=[(host, port)]))
    return out


def group_proxies(proxies: list[XrayProxy]) -> list[Location]:
    """Локации из подписки xray-checker: группировка по ``groupName``, порядок — как в ответе."""

    grouped: dict[str, Location] = {}
    used: set[str] = set()
    for p in proxies:
        name = p.group or p.name or f"{p.server}:{p.port}"
        loc = grouped.get(name)
        if loc is None:
            loc = Location(key=unique_key(name, used), name=name)
            grouped[name] = loc
        target = (p.server, p.port)
        if target not in loc.targets:
            loc.targets.append(target)
    return list(grouped.values())


async def fetch_locations(
    checker: WorldSource | None, manual: tuple[str, ...] | list[str]
) -> list[Location]:
    """Локации: из ``TARGETS``, если он задан, иначе из подписки xray-checker.

    Ошибка источника — исключение наружу: решение «показать нет данных» принимает
    сборщик, здесь важно не потерять различие «источник упал» и «целей нет».
    """

    if manual:
        return parse_targets(manual)
    if checker is None:
        return []
    proxies = await checker.fetch()
    return group_proxies(proxies)


__all__ = [
    "Location",
    "fetch_locations",
    "group_proxies",
    "parse_target",
    "parse_targets",
    "slug",
    "unique_key",
]
