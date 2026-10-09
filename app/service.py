"""Сборка публичного ответа ``/api/status``.

НИКОГДА не отдаём адреса, порты, домены серверов и id измерений Globalping —
страница не должна превращаться в список целей для блокировки. Исключение —
явно включённый ``EXPOSE_TARGETS``: тогда у локации есть ``targets``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import store
from .collector import StatusState
from .uptime import availability_pct


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def overall_status(levels: list[str]) -> str:
    known = [lvl for lvl in levels if lvl != "unknown"]
    if not known:
        return "unknown"
    if all(lvl == "red" for lvl in known):
        return "down"
    if any(lvl in ("red", "yellow") for lvl in known):
        return "degraded"
    return "ok"


def median_ms(rows: list[dict]) -> int | None:
    """Медиана задержки по зондам РФ, ответившим на последнем замере (мс)."""

    values = sorted(
        float(r["ms"]) for r in rows
        if isinstance(r, dict) and r.get("ok") and isinstance(r.get("ms"), (int, float))
    )
    if not values:
        return None
    mid = len(values) // 2
    med = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return int(round(med))


def uptime_info(checks: list[tuple[str, int, str]]) -> dict[str, Any]:
    """Доступность за весь период с данными: ``pct`` и ``hours`` (длина периода).

    Нет проверок с данными — ``pct = None``; период короче часа тоже не показываем
    (``pct = None``), чтобы не рисовать цифру по паре замеров.
    """

    known = [c for c in checks if c[2] != "unknown"]
    pct = availability_pct(known)
    if pct is None:
        return {"pct": None, "hours": 0.0}
    hours = (max(c[1] for c in known) - min(c[1] for c in known)) / 3600
    if hours < 1:
        return {"pct": None, "hours": round(hours, 2)}
    return {"pct": round(pct, 1), "hours": round(hours, 1)}


def disabled_payload() -> dict[str, Any]:
    return {"updated_at": None, "overall": "unknown", "locations": []}


async def build_status(
    state: StatusState,
    sessionmaker,
    *,
    expose_targets: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    latest = await store.latest_checks(sessionmaker)
    history = await store.check_history(sessionmaker, days=30, now=moment)
    days = store.day_levels_from(history)
    day_keys = [
        (moment - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)
    ]

    out: list[dict[str, Any]] = []
    levels: list[str] = []
    newest: int | None = None
    for loc in state.locations:
        w = latest.get((loc.key, "world"))
        r = latest.get((loc.key, "ru"))
        world = (
            {
                "level": w.level,
                "checked_at": _iso(w.checked_at),
                "latency_ms": w.latency_ms,
                "ok": w.ok_count,
                "total": w.total_count,
            }
            if w else None
        )
        ru = None
        if r is not None:
            got = await store.get_probes(sessionmaker, loc.key)
            ru = {
                "level": r.level,
                "ok": r.ok_count,
                "total": r.total_count,
                "median_ms": median_ms(got[1]) if got else None,
                "checked_at": _iso(r.checked_at),
            }
        for item in (w, r):
            if item is not None:
                levels.append(item.level)
                newest = item.checked_at if newest is None else max(newest, item.checked_at)
        per_day = days.get(loc.key, {})
        item: dict[str, Any] = {
            "key": loc.key,
            "name": loc.name,
            "world": world,
            "ru": ru,
            "uptime": uptime_info(history.get(loc.key, [])),
            "days": [{"date": d, "level": per_day.get(d, "unknown")} for d in day_keys],
        }
        if expose_targets:
            item["targets"] = [{"host": h, "port": p} for h, p in loc.targets]
        out.append(item)
    return {"updated_at": _iso(newest), "overall": overall_status(levels), "locations": out}


async def build_providers(sessionmaker, key: str) -> dict[str, Any]:
    """Построчно по зондам последнего замера РФ: город, провайдер, доступен ли, мс."""

    got = await store.get_probes(sessionmaker, key)
    if got is None:
        return {"checked_at": None, "rows": []}
    checked_at, rows = got
    return {
        "checked_at": _iso(checked_at),
        "rows": [
            {
                "city": str(r.get("city") or ""),
                "provider": str(r.get("provider") or ""),
                "asn": r.get("asn") if isinstance(r.get("asn"), int) else None,
                "ok": bool(r.get("ok")),
                "ms": r.get("ms"),
            }
            for r in rows
            if isinstance(r, dict)
        ],
    }
