"""Уровень дня и процент доступности по истории проверок (``status_check``).

Правила (пороги — константы ниже):

* Проверка «сбойная» (red) — сервер не отвечает из мира или у <50 % зондов РФ;
  «частичная» (yellow) — у части провайдеров РФ адрес не открывается;
  ``unknown`` («нет данных») в расчёт не входит вообще.
* Доступность = доля НЕ красных проверок среди проверок с данными.
* День: ``red`` — доступность < ``DAY_RED_BELOW_PCT`` ИЛИ был непрерывный простой
  (красные проверки подряд у одного источника) не короче ``DAY_OUTAGE_MINUTES``;
  ``yellow`` — доля красных+жёлтых проверок ≥ ``DAY_ISSUE_MIN_PCT``;
  ``green`` — иначе; ``unknown`` — нет проверок с данными.
"""

from __future__ import annotations

from collections.abc import Iterable

#: День красный, если доступность за день ниже этого процента.
DAY_RED_BELOW_PCT = 90.0
#: Непрерывный простой не короче этого (минуты) делает день красным.
DAY_OUTAGE_MINUTES = 60
#: Доля проблемных (red+yellow) проверок, с которой день уже жёлтый.
DAY_ISSUE_MIN_PCT = 1.0

#: (источник, unix-время, уровень)
Check = tuple[str, int, str]


def availability_pct(checks: Iterable[Check]) -> float | None:
    """Доля не-красных проверок среди проверок с данными, %; ``None`` — данных нет."""

    known = [lvl for _, _, lvl in checks if lvl != "unknown"]
    if not known:
        return None
    return 100.0 * sum(1 for lvl in known if lvl != "red") / len(known)


def longest_outage_minutes(checks: Iterable[Check]) -> float:
    """Самая длинная серия красных проверок подряд у одного источника, минуты.

    Проверки ``unknown`` серию не рвут и не продлевают (данных нет), зелёные и
    жёлтые — рвут. Длина — от первой до последней красной проверки серии.
    """

    best = 0
    by_source: dict[str, list[tuple[int, str]]] = {}
    for src, ts, lvl in checks:
        if lvl != "unknown":
            by_source.setdefault(src, []).append((ts, lvl))
    for rows in by_source.values():
        rows.sort()
        start: int | None = None
        last = 0
        for ts, lvl in rows:
            if lvl == "red":
                if start is None:
                    start = ts
                last = ts
                best = max(best, last - start)
            else:
                start = None
    return best / 60.0


def day_level(checks: list[Check]) -> str:
    """Уровень дня по всем проверкам локации за этот день."""

    known = [c for c in checks if c[2] != "unknown"]
    if not known:
        return "unknown"
    avail = availability_pct(known)
    if avail is not None and avail < DAY_RED_BELOW_PCT:
        return "red"
    if longest_outage_minutes(known) >= DAY_OUTAGE_MINUTES:
        return "red"
    issues = sum(1 for c in known if c[2] in ("red", "yellow"))
    if 100.0 * issues / len(known) >= DAY_ISSUE_MIN_PCT:
        return "yellow"
    return "green"
