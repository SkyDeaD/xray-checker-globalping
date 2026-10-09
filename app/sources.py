"""Источники проверок: xray-checker («из мира») и Globalping («из России»).

Сверено по исходникам/спецификациям (06.10.2026):

* xray-checker ``web/api.go``, ``main.go``: ``GET /api/v1/proxies`` отдаёт
  ``{"success": true, "data": [{stableId, name, server, port, protocol, online,
  latencyMs, lastCheck, groupName, subName}]}``. Эндпоинт защищён Basic Auth при
  ``METRICS_PROTECTED=true`` (логин/пароль — ``METRICS_USERNAME``/``METRICS_PASSWORD``).
  Публичный ``/api/v1/public/proxies`` полей server/port НЕ содержит — он нам не
  подходит. ``server``/``port`` отдаются всегда (``WEB_SHOW_DETAILS`` относится
  только к ``generatedConfig``).
* Globalping https://api.globalping.io/v1/spec.yaml: ``POST /v1/measurements`` → 202
  ``{id, probesCount}``; ``GET /v1/measurements/{id}``, ``status``:
  ``in-progress|finished``; у каждого зонда ``probe.{city,network,asn,...}`` и
  ``result.status`` ``finished|failed|offline|in-progress``; у finished ping —
  ``result.stats.{avg,loss,rcv,total}``. На 429 — заголовки ``Retry-After``
  и/или ``X-RateLimit-Reset`` (секунды).

Токены/пароли и id измерений в лог не пишутся.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

GLOBALPING_BASE = "https://api.globalping.io"

#: Тег зондов Globalping: домашние провайдеры, а не дата-центры.
EYEBALL_TAG = "eyeball-network"


class SourceError(Exception):
    """Источник недоступен или ответил неожиданно — у локации будет «нет данных»."""


class RateLimitedError(SourceError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


# --------------------------------------------------------------------------- xray-checker


@dataclass(frozen=True)
class XrayProxy:
    server: str
    port: int
    online: bool
    latency_ms: int
    #: Группа подписки (``groupName``) и имя прокси (``name``) — из них собираются локации.
    group: str = ""
    name: str = ""


class WorldSource(Protocol):
    async def fetch(self) -> list[XrayProxy]: ...

    async def aclose(self) -> None: ...


class XrayCheckerSource:
    def __init__(
        self,
        url: str,
        user: str = "",
        password: str = "",
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        auth = httpx.BasicAuth(user, password) if user or password else None
        self._client = httpx.AsyncClient(timeout=timeout, auth=auth, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self) -> list[XrayProxy]:
        try:
            resp = await self._client.get(f"{self._url}/api/v1/proxies")
        except httpx.HTTPError as exc:
            raise SourceError(f"xray-checker: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceError(f"xray-checker: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise SourceError("xray-checker: ответ не JSON") from exc
        data = body.get("data") if isinstance(body, dict) else body
        if not isinstance(data, list):
            raise SourceError("xray-checker: неожиданная форма ответа")
        proxies: list[XrayProxy] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            server = str(item.get("server") or "").strip()
            try:
                port = int(item.get("port"))
            except (TypeError, ValueError):
                continue
            if not server:
                continue
            try:
                latency = int(item.get("latencyMs") or 0)
            except (TypeError, ValueError):
                latency = 0
            proxies.append(
                XrayProxy(
                    server,
                    port,
                    bool(item.get("online")),
                    latency,
                    group=str(item.get("groupName") or "").strip(),
                    name=str(item.get("name") or "").strip(),
                )
            )
        return proxies


# --------------------------------------------------------------------------- Globalping


@dataclass(frozen=True)
class ProbeRow:
    city: str
    provider: str
    asn: int | None
    ok: bool
    ms: int | None


class RuSource(Protocol):
    async def measure(self, target: str, port: int, limit: int) -> list[ProbeRow]: ...

    async def aclose(self) -> None: ...


def _retry_after(resp: httpx.Response, default: float = 60.0) -> float:
    for header in ("retry-after", "x-ratelimit-reset"):
        raw = resp.headers.get(header)
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return default


def parse_probe_rows(results: list[Any]) -> list[ProbeRow]:
    """Строки по зондам. Внутренние сбои Globalping и offline/in-progress не считаем.

    ``finished`` — доступен, если получен хоть один ответ (``rcv > 0``, иначе
    ``loss < 100``). ``failed`` с источником не ``internal`` — недоступен.
    """

    rows: list[ProbeRow] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        probe = item.get("probe") or {}
        result = item.get("result") or {}
        status = result.get("status")
        if status == "finished":
            stats = result.get("stats") or {}
            rcv = stats.get("rcv")
            if isinstance(rcv, (int, float)):
                ok = rcv > 0
            else:
                loss = stats.get("loss")
                ok = isinstance(loss, (int, float)) and loss < 100
            avg = stats.get("avg")
            ms = int(round(avg)) if ok and isinstance(avg, (int, float)) else None
        elif status == "failed" and result.get("failureSource") != "internal":
            ok, ms = False, None
        else:
            continue
        asn = probe.get("asn")
        rows.append(
            ProbeRow(
                city=str(probe.get("city") or ""),
                provider=str(probe.get("network") or ""),
                asn=asn if isinstance(asn, int) else None,
                ok=ok,
                ms=ms,
            )
        )
    return rows


class GlobalpingSource:
    """TCP-ping порта с зондов у домашних провайдеров выбранной страны.

    Проверяется только «открывается ли адрес и порт»: ни VLESS/Reality, ни
    Hysteria2 (UDP) этим способом не проверить. См. README.
    """

    MAX_WAIT = 300.0
    POST_RETRIES = 2

    def __init__(
        self,
        token: str,
        *,
        country: str = "RU",
        tags: tuple[str, ...] = (EYEBALL_TAG,),
        base_url: str = GLOBALPING_BASE,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._country = country
        self._tags = list(tags)
        self._timeout = timeout
        self._poll = poll_interval
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise SourceError(f"globalping: {type(exc).__name__}") from exc

    def _locations(self) -> list[dict[str, Any]]:
        loc: dict[str, Any] = {"country": self._country}
        if self._tags:
            loc["tags"] = self._tags
        return [loc]

    async def _create(self, target: str, port: int, limit: int) -> str:
        body = {
            "type": "ping",
            "target": target,
            "locations": self._locations(),
            "limit": limit,
            "measurementOptions": {"protocol": "TCP", "port": port, "packets": 3},
        }
        for attempt in range(self.POST_RETRIES + 1):
            resp = await self._request("POST", f"{self._base}/v1/measurements", json=body)
            if resp.status_code == 429:
                delay = min(_retry_after(resp), self.MAX_WAIT)
                if attempt >= self.POST_RETRIES:
                    raise RateLimitedError(delay)
                logger.warning("Globalping: 429, жду %.0f с", delay)
                await self._sleep(delay)
                continue
            if resp.status_code != 202:
                raise SourceError(f"globalping: POST HTTP {resp.status_code}")
            try:
                mid = resp.json()["id"]
            except (ValueError, KeyError, TypeError) as exc:
                raise SourceError("globalping: в ответе нет id") from exc
            return str(mid)
        raise SourceError("globalping: недостижимо")  # pragma: no cover

    async def measure(self, target: str, port: int, limit: int) -> list[ProbeRow]:
        mid = await self._create(target, port, limit)
        url = f"{self._base}/v1/measurements/{mid}"
        elapsed = 0.0
        while elapsed <= self._timeout:
            await self._sleep(self._poll)  # лимит GET — 2/с на измерение
            elapsed += self._poll
            resp = await self._request("GET", url)
            if resp.status_code == 429:
                delay = min(_retry_after(resp, self._poll), self.MAX_WAIT)
                await self._sleep(delay)
                elapsed += delay
                continue
            if resp.status_code != 200:
                raise SourceError(f"globalping: GET HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError("globalping: ответ не JSON") from exc
            if data.get("status") != "in-progress":
                return parse_probe_rows(data.get("results") or [])
        raise SourceError("globalping: таймаут ожидания измерения")


# --------------------------------------------------------------------------- бюджет квоты

#: Квота 500 тестов/ч (тест = 1 зонд); держим запас.
RU_BUDGET_PER_HOUR = 450


def effective_ru_interval(locations: int, limit: int, interval: int) -> int:
    """Интервал замеров РФ, секунды, не нарушающий бюджет ``RU_BUDGET_PER_HOUR``."""

    if locations <= 0 or limit <= 0:
        return interval
    needed = math.ceil(locations * limit * 3600 / RU_BUDGET_PER_HOUR)
    return max(interval, needed)
