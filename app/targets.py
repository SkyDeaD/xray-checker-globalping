"""Локации: что проверяем и как это называется в публичном API.

Локация — группа целей ``(host, port)`` с одним именем. Источников три, берётся
первый настроенный:

1. ``TARGETS="Имя=host:port,..."`` — вручную, по одной цели на локацию. Явное
   указание важнее остальных источников.
2. панель Remnawave (``PANEL_URL`` + ``PANEL_TOKEN``) — локация = страна, цели =
   адреса её хостов. Новая нода в панели = новая цель, править код не нужно. Хост, чей inbound входит в
   ``WHITELIST_SQUAD_UUID``, помечается как «белые списки» и считается отдельной
   локацией (``NL-wl``).
3. подписка xray-checker (``CHECKER_URL``) — локация = группа подписки
   (``groupName`` прокси), цели = адреса и порты серверов группы.

Ключ локации — читаемый латинский слаг имени (``Amsterdam`` → ``amsterdam``); для
имён без латиницы (``Нидерланды``) ключ — стабильный ``loc-`` + 8 hex от имени,
чтобы он не зависел от порядка целей. Из панели ключ — код страны (``NL``).
Ключ попадает в URL ``/api/status/{key}/providers``.

Адреса и порты целей живут только в памяти сборщика и в публичный API не
попадают, пока не включён ``EXPOSE_TARGETS``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .countries import COUNTRY_RU
from .panel import RemnawavePanel
from .sources import SourceError, WorldSource, XrayProxy

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
#: Максимальная длина ключа (по DDL: VARCHAR(16)).
KEY_MAX = 16
_FAR = 10**9


@dataclass
class Location:
    key: str
    name: str
    #: (host, port) — НАРУЖУ НЕ ОТДАЁТСЯ, пока не включён EXPOSE_TARGETS.
    targets: list[tuple[str, int]] = field(default_factory=list)
    #: Заполняются только панелью: код страны и признак «белых списков».
    country_code: str = ""
    whitelist: bool = False
    #: Порядок из панели (``viewPosition`` хоста) — чтобы порядок был как в панели.
    view_position: int = _FAR


# --------------------------------------------------------------------------- ключи


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


# --------------------------------------------------------------------------- TARGETS


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


def parse_targets(specs: Iterable[str]) -> list[Location]:
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


# --------------------------------------------------------------------------- подписка


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


# --------------------------------------------------------------------------- панель


def _nodes_map(nodes_raw: Any) -> dict[str, dict[str, Any]]:
    items = nodes_raw if isinstance(nodes_raw, list) else (nodes_raw or {}).get("nodes", [])
    return {str(n.get("uuid")): n for n in items if isinstance(n, dict) and n.get("uuid")}


def _host_position(host: dict[str, Any]) -> int:
    try:
        return int(host.get("viewPosition"))
    except (TypeError, ValueError):
        return _FAR


def parse_panel_locations(
    hosts: list[dict[str, Any]],
    nodes_raw: Any,
    squad_inbounds: dict[str, set[str]] | None = None,
    whitelist_squad_uuid: str = "",
) -> list[Location]:
    """Локации из хостов панели; результат — в порядке ``viewPosition``.

    Правило отбора хоста:

    * хост не выключен, у него есть адрес и корректный порт;
    * у хоста есть ноды (служебные хосты-заглушки нод не имеют — отпадают сами);
    * inbound хоста входит в активные inbound'ы одной из его (не выключенных) нод,
      и тип этого inbound — ``vless`` (Hysteria2 так не проверить: она по UDP);
    * код страны ноды известен таблице ``COUNTRY_RU``; иначе хост пропускается
      с предупреждением — лучше не показать, чем показать выдуманное название.

    Хост, чей inbound входит в squad ``whitelist_squad_uuid``, попадает в
    отдельную локацию с суффиксом ``-wl`` и флагом ``whitelist``.
    """

    nodes = _nodes_map(nodes_raw)
    wl_inbounds = (squad_inbounds or {}).get((whitelist_squad_uuid or "").strip(), set())
    result: dict[str, Location] = {}
    unknown: set[str] = set()

    for host in sorted((h for h in hosts if isinstance(h, dict)), key=_host_position):
        if host.get("isDisabled"):
            continue
        address = str(host.get("address") or "").strip()
        try:
            port = int(host.get("port"))
        except (TypeError, ValueError):
            continue
        if not address or not 0 < port < 65536:
            continue

        node_uuids = [str(u) for u in host.get("nodes") or []]
        if not node_uuids:
            continue
        inbound = host.get("inbound")
        inbound_uuid = ""
        if isinstance(inbound, dict):
            inbound_uuid = str(inbound.get("configProfileInboundUuid") or "")
        if not inbound_uuid:
            continue

        # Первая подходящая нода (в порядке хоста) определяет страну.
        matched: dict[str, Any] | None = None
        for node_uuid in node_uuids:
            node = nodes.get(node_uuid)
            if node is None or node.get("isDisabled"):
                continue
            active = (node.get("configProfile") or {}).get("activeInbounds") or []
            for ib in active:
                if isinstance(ib, dict) and str(ib.get("uuid") or "") == inbound_uuid:
                    if str(ib.get("type") or "").lower() == "vless":
                        matched = node
                    break
            if matched is not None:
                break
        if matched is None:
            continue

        code = str(matched.get("countryCode") or "").strip().upper()
        name = COUNTRY_RU.get(code)
        if name is None:
            unknown.add(code or "<без кода>")
            continue

        whitelist = inbound_uuid in wl_inbounds
        key = f"{code}-wl" if whitelist else code
        loc = result.get(key)
        if loc is None:
            loc = result[key] = Location(
                key=key,
                name=name,
                country_code=code,
                whitelist=whitelist,
                view_position=_host_position(host),
            )
        if (address, port) not in loc.targets:
            loc.targets.append((address, port))

    if unknown:
        logger.warning(
            "Статус: коды стран не распознаны, локации пропущены: %s", ", ".join(sorted(unknown))
        )
    return sorted(result.values(), key=lambda loc: (loc.view_position, loc.key))


# --------------------------------------------------------------------------- источники


class TargetSource(Protocol):
    """Откуда берём список локаций."""

    #: Человеческое имя источника — только для лога при старте.
    name: str

    async def fetch(self) -> list[Location]: ...

    async def aclose(self) -> None: ...


class ManualTargets:
    """``TARGETS`` из окружения."""

    name = "TARGETS (свои цели)"

    def __init__(self, specs: Iterable[str]) -> None:
        self._specs = list(specs)

    async def fetch(self) -> list[Location]:
        return parse_targets(self._specs)

    async def aclose(self) -> None:
        return None


class PanelTargets:
    """Хосты панели Remnawave. Клиент создаётся здесь и закрывается здесь же."""

    name = "панель Remnawave"

    def __init__(self, panel: RemnawavePanel, whitelist_squad_uuid: str = "") -> None:
        self._panel = panel
        self._wl_squad = whitelist_squad_uuid

    async def fetch(self) -> list[Location]:
        hosts = await self._panel.fetch_hosts()
        nodes = await self._panel.fetch_nodes()
        squads = await self._panel.fetch_squad_inbounds() if self._wl_squad else {}
        return parse_panel_locations(hosts, nodes, squads, self._wl_squad)

    async def aclose(self) -> None:
        await self._panel.aclose()


class CheckerTargets:
    """Локации из подписки, которую проверяет xray-checker.

    Клиента не закрывает: он общий с проверками «из мира», закрывает его тот,
    кто создал (см. ``app/api.py``).
    """

    name = "подписка xray-checker"

    def __init__(self, checker: WorldSource) -> None:
        self._checker = checker

    async def fetch(self) -> list[Location]:
        return group_proxies(await self._checker.fetch())

    async def aclose(self) -> None:
        return None


def build_targets(
    *,
    checker: WorldSource | None,
    panel_url: str = "",
    panel_token: str = "",
    whitelist_squad_uuid: str = "",
    manual: Iterable[str] = (),
) -> TargetSource | None:
    """Выбрать источник локаций: ``TARGETS`` → панель → подписка. ``None`` — не из чего."""

    specs = list(manual)
    if specs:
        return ManualTargets(specs)
    if panel_url and panel_token:
        return PanelTargets(RemnawavePanel(panel_url, panel_token), whitelist_squad_uuid)
    if checker is not None:
        return CheckerTargets(checker)
    return None


__all__ = [
    "CheckerTargets",
    "Location",
    "ManualTargets",
    "PanelTargets",
    "SourceError",
    "TargetSource",
    "build_targets",
    "group_proxies",
    "parse_panel_locations",
    "parse_target",
    "parse_targets",
    "slug",
    "unique_key",
]
