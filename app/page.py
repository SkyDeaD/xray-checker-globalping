"""Готовая страница статуса: один HTML, без сборки, без JS и без внешних файлов.

Страница рендерится на сервере из того же ответа, что отдаёт ``/api/status``
(плюс построчные данные по провайдерам). Ни карты, ни чужого брендинга: только
таблица — локация, «из мира», «из России», полосы аптайма за 30 дней и
раскрывающийся разбор по провайдерам.

Адреса и порты серверов на страницу не попадают: только имена локаций.
"""

from __future__ import annotations

import html
from string import Template
from typing import Any

#: Подписи уровней — те же значения, что в API (``level``).
LEVEL_RU = {
    "green": "доступен",
    "yellow": "частично",
    "red": "недоступен",
    "unknown": "нет данных",
}
OVERALL_RU = {
    "ok": "всё в порядке",
    "degraded": "есть проблемы",
    "down": "недоступно",
    "unknown": "нет данных",
}

PAGE = Template(
    """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Статус серверов</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1f2328;
  --muted: #656d76;
  --line: #d8dee4;
  --card: #f6f8fa;
  --green: #1a7f37;
  --yellow: #9a6700;
  --red: #cf222e;
  --unknown: #8c959f;
  --empty: #8c959f;   /* рамка пустой клетки: контраст 3.04:1 к белому */
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #9198a1;
    --line: #30363d;
    --card: #161b22;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --unknown: #6e7681;
    --empty: #5b636d;   /* контраст 3.11:1 к фону темы */
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 16px 48px;
  background: var(--bg);
  color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
}
main { max-width: 900px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
p.meta { color: var(--muted); margin: 0 0 16px; font-size: 14px; }
p.note, p.foot { color: var(--muted); font-size: 13px; }
p.foot { margin-top: 20px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 13px; color: var(--muted); font-weight: 600; white-space: nowrap; }
td.name { font-weight: 600; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 13px; white-space: nowrap; }
.badge.green { color: var(--green); background: color-mix(in srgb, var(--green) 12%, transparent); }
.badge.yellow { color: var(--yellow); background: color-mix(in srgb, var(--yellow) 14%, transparent); }
.badge.red { color: var(--red); background: color-mix(in srgb, var(--red) 12%, transparent); }
.badge.unknown { color: var(--unknown); background: color-mix(in srgb, var(--unknown) 14%, transparent); }
.dim { color: var(--muted); font-size: 13px; }
.mark { display: block; font-weight: 400; font-size: 12px; color: var(--muted); }
.bars { display: inline-flex; gap: 2px; align-items: flex-end; height: 24px; }
.bar { width: 6px; height: 20px; border-radius: 1px; background: var(--unknown); }
.bar.green { background: var(--green); }
.bar.yellow { background: var(--yellow); }
.bar.red { background: var(--red); }
/* «нет данных» — пустая клетка, а не заливка: иначе полоса читается как сплошная */
.bar.unknown { background: transparent; box-shadow: inset 0 0 0 1px var(--empty); }
.scroll { overflow-x: auto; }
details { margin: 2px 0 6px; }
summary { cursor: pointer; color: var(--muted); font-size: 13px; }
details table { margin: 8px 0 4px; background: var(--card); border-radius: 8px; }
details th, details td { border-bottom: 1px solid var(--line); font-size: 13px; padding: 6px 8px; }
details tr:last-child td { border-bottom: 0; }
.empty { padding: 28px 12px; text-align: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; }
</style>
</head>
<body>
<main>
<h1>Статус серверов</h1>
<p class="meta">Обновлено: $updated &middot; Итог: $overall</p>
<p class="note">
«Из мира» — проверка настоящим клиентом через сервер (xray-checker).
«Из России» — зонды Globalping у домашних провайдеров: открывается ли адрес и порт сервера.
Сам протокол (VLESS/Reality) и блокировку по DPI такая проверка не видит: если сервер
доступен из России, а подключение не идёт — это тот случай.
</p>
$body
<p class="foot"><b>Полоса доступности</b> — 30 дней: слева самый старый день, справа сегодня.
Пустая клетка — нет данных, зелёная — доступен, жёлтая — частично, красная — недоступен.
Наведите на клетку, чтобы увидеть дату.
Зелёный — от 80&nbsp;% ответивших российских зондов, жёлтый — от 50&nbsp;%, красный — ниже.
«Нет данных» означает, что источник не ответил или зонды не взялись за задачу, а не
«сервер недоступен».</p>
</main>
</body>
</html>
"""
)

PROVIDERS = Template(
    """<details>
<summary>По провайдерам в России: $ok из $total доступны</summary>
<table>
<thead><tr><th>Город</th><th>Провайдер</th><th>ASN</th><th>Результат</th><th>Задержка</th></tr></thead>
<tbody>
$rows</tbody>
</table>
</details>"""
)

PROVIDER_ROW = Template(
    """<tr><td>$city</td><td>$provider</td><td>$asn</td><td>$result</td><td>$ms</td></tr>
"""
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(level: str, label: str | None = None) -> str:
    safe = level if level in LEVEL_RU else "unknown"
    return f'<span class="badge {safe}">{_esc(label or LEVEL_RU[safe])}</span>'


def _bars(days: list[dict[str, Any]]) -> str:
    """Полосы аптайма: по столбику на день, последние 30 (старые слева)."""

    cells = []
    for day in days:
        level = str(day.get("level") or "unknown")
        safe = level if level in LEVEL_RU else "unknown"
        title = f"{day.get('date') or ''}: {LEVEL_RU[safe]}"
        cells.append(f'<span class="bar {safe}" title="{_esc(title)}"></span>')
    return f'<span class="bars">{"".join(cells)}</span>'


def _cell(source: dict[str, Any] | None) -> str:
    """Ячейка «из мира» или «из России»."""

    if not source:
        return _badge("unknown")
    level = str(source.get("level") or "unknown")
    if level == "unknown":
        return _badge("unknown")
    ok, total = source.get("ok"), source.get("total")
    if isinstance(ok, int) and isinstance(total, int) and total > 0:
        label = f"{ok} из {total}"
    else:
        label = LEVEL_RU.get(level, "нет данных")
    extra = ""
    ms = source.get("latency_ms") if "latency_ms" in source else source.get("median_ms")
    if isinstance(ms, int):
        extra = f'<div class="dim">{"задержка" if source.get("latency_ms") else "медиана"} {ms} мс</div>'
    return _badge(level, label) + extra


def _providers(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    body = []
    for row in rows:
        ms = row.get("ms")
        body.append(
            PROVIDER_ROW.substitute(
                city=_esc(row.get("city")),
                provider=_esc(row.get("provider")),
                asn=_esc(row.get("asn") if row.get("asn") is not None else "—"),
                result=_badge("green" if row.get("ok") else "red", "доступен" if row.get("ok") else "нет"),
                ms=_esc(f"{ms} мс" if isinstance(ms, int) else "—"),
            )
        )
    return PROVIDERS.substitute(
        ok=sum(1 for row in rows if row.get("ok")),
        total=len(rows),
        rows="".join(body),
    )


def _location(loc: dict[str, Any], providers: list[dict[str, Any]]) -> str:
    uptime = loc.get("uptime") or {}
    pct = uptime.get("pct")
    hours = uptime.get("hours")
    availability = (
        f'<div class="dim">{pct} % за {hours} ч</div>' if isinstance(pct, (int, float)) else ""
    )
    mark = '<span class="mark">белые списки</span>' if loc.get("whitelist") else ""
    return (
        "<tr>"
        f'<td class="name">{_esc(loc.get("name"))}{mark}</td>'
        f'<td>{_cell(loc.get("world"))}</td>'
        f'<td>{_cell(loc.get("ru"))}</td>'
        f'<td>{_bars(loc.get("days") or [])}{availability}</td>'
        "</tr>"
        f'<tr><td colspan="4">{_providers(providers)}</td></tr>'
    )


def render_page(payload: dict[str, Any], providers: dict[str, list[dict[str, Any]]]) -> str:
    """HTML страницы. ``providers`` — построчные данные по зондам РФ по ключу локации."""

    locations = payload.get("locations") or []
    rows = "".join(
        _location(loc, providers.get(str(loc.get("key")), [])) for loc in locations
    )
    if rows:
        body = (
            '<div class="scroll"><table><thead><tr><th>Локация</th><th>Из мира</th>'
            f"<th>Из России</th><th>Доступность, 30 дней</th></tr></thead><tbody>{rows}"
            "</tbody></table></div>"
        )
    else:
        body = (
            '<div class="empty">Проверок пока нет. Появятся через несколько минут после '
            "запуска, если сервису есть откуда взять серверы: панель (PANEL_URL), "
            "свои цели (TARGETS) или подписка xray-checker. Для проверок из России "
            "нужен токен Globalping — без него будет только колонка «из мира».</div>"
        )
    updated = payload.get("updated_at")
    overall = str(payload.get("overall") or "unknown")
    return PAGE.substitute(
        body=body,
        updated=_esc(updated) if updated else "ещё не было",
        overall=_esc(OVERALL_RU.get(overall, overall)),
    )
