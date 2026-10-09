"""Клиент API панели Remnawave — только чтение, три запроса.

Нужен ровно для одного: взять список серверов (хостов) и понять, к какой стране
относится каждый — чтобы проверить эти адреса из России. Ничего не меняем.

Контракт (Remnawave 3.x, проверен по рабочей панели):

* ``GET /api/hosts`` → ``{"response": [{address, port, isDisabled, nodes[],
  inbound.configProfileInboundUuid, viewPosition, remark, ...}]}``;
* ``GET /api/nodes`` → ``{"response": {"nodes": [{uuid, countryCode, isDisabled,
  configProfile.activeInbounds[{uuid, type}]}]}}``;
* ``GET /api/internal-squads`` → ``{"response": {"internalSquads": [{uuid,
  inbounds[{uuid}]}]}}`` — нужен лишь чтобы отметить локацию «белыми списками»,
  если вы укажете ``WHITELIST_SQUAD_UUID``;
* авторизация — ``Authorization: Bearer <token>`` (и ``X-Api-Key`` для старых
  сборок), плюс обязательные ``X-Forwarded-*``: за прокси панель без них
  отвечает 403. Токен в лог не пишется.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Временные сбои панели/прокси — повторяем.
RETRY_STATUS = {429, 502, 503, 504}


class PanelError(Exception):
    """Панель недоступна или ответила неожиданно."""

    def __init__(self, msg: str, *, status_code: int | None = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.status_code = status_code


class PanelAuthError(PanelError):
    """Неверный токен или не прошли forwarded-заголовки (HTTP 401/403)."""


class RemnawavePanel:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff = backoff_base
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Api-Key": token,
                "Accept": "application/json",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, path: str) -> Any:
        """GET с ретраями; возвращает объект из конверта ``{"response": ...}``."""

        url = f"{self._base}{path}"
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff * (2**attempt))
                    continue
                raise PanelError(f"панель: {type(exc).__name__}") from exc

            if resp.status_code in RETRY_STATUS and attempt < self._max_retries:
                raw = resp.headers.get("Retry-After")
                try:
                    delay = float(raw) if raw else self._backoff * (2**attempt)
                except ValueError:
                    delay = self._backoff * (2**attempt)
                await self._sleep(delay)
                continue
            if resp.status_code in (401, 403):
                raise PanelAuthError(f"панель: HTTP {resp.status_code} (токен или forwarded-заголовки)")
            if resp.status_code != 200:
                raise PanelError(f"панель: HTTP {resp.status_code}", status_code=resp.status_code)
            body = resp.json()
            if isinstance(body, dict) and "response" in body:
                return body["response"]
            return body
        raise PanelError("панель: недостижимо")  # pragma: no cover

    async def fetch_hosts(self) -> list[dict[str, Any]]:
        data = await self._request("/api/hosts")
        hosts = data if isinstance(data, list) else (data or {}).get("hosts", [])
        return [h for h in hosts if isinstance(h, dict)]

    async def fetch_nodes(self) -> list[dict[str, Any]]:
        data = await self._request("/api/nodes")
        nodes = data if isinstance(data, list) else (data or {}).get("nodes", [])
        return [n for n in nodes if isinstance(n, dict)]

    async def fetch_squad_inbounds(self) -> dict[str, set[str]]:
        """``{uuid squad'а: {uuid входящих подключений}}`` — чтобы отметить белые списки."""

        data = await self._request("/api/internal-squads")
        squads = data.get("internalSquads", []) if isinstance(data, dict) else []
        result: dict[str, set[str]] = {}
        for squad in squads:
            if not isinstance(squad, dict):
                continue
            uuid = str(squad.get("uuid") or "")
            if not uuid:
                continue
            result[uuid] = {
                str(inbound.get("uuid"))
                for inbound in squad.get("inbounds") or []
                if isinstance(inbound, dict) and inbound.get("uuid")
            }
        return result
