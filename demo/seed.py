"""История за 30 дней для демо-стенда — через ``app.store.add_check``, а не сырым SQL.

Свежая установка выглядит пусто: почти все дни в полосе — «нет данных». Чтобы на
демо было видно, как страница выглядит на обжитой установке, кладём синтетические
проверки за 30 дней: у каждой локации свой характер (ровно зелёная, с простоями,
закрытая из России).

Данные вымышленные. Реальный сборщик начинает поверх них писать настоящие замеры.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from app import store
from app.collector import ru_level
from app.db import bootstrap, make_engine, make_sessionmaker

STEP = timedelta(minutes=30)

#: Ключи локаций — те же, что даёт панель из demo/stub.py.
LOCATIONS = ["NL", "NL-wl", "PL", "DE"]


def world_counts(loc: str, day: int, hour: int) -> tuple[int, int]:
    """«Из мира»: один сервер; у Германии на 6-й день назад был трёхчасовой простой."""

    if loc == "DE" and day == 6 and 2 <= hour < 5:
        return 0, 1
    return 1, 1


def ru_counts(loc: str, day: int, hour: int) -> tuple[int, int]:
    """«Из России»: сколько зондов из 20 увидели адрес."""

    if loc == "NL":
        if day in (12, 13) and 2 <= hour < 7:
            return 0, 20               # адрес перестал открываться совсем
        if day % 3 == 0 and 9 <= hour < 10:
            return 13, 20              # часть провайдеров
        return 20, 20
    if loc == "NL-wl":
        if 13 <= hour < 15:
            return 11, 20
        return 18, 20
    if loc == "PL":
        if day in (4, 5):
            return 9, 20               # крупный провайдер блокирует
        if 12 <= hour < 14:
            return 11, 20
        return 15, 20
    return 4, 20                       # Германия: из России почти всегда закрыта


def latency(loc: str, day: int, hour: int) -> int:
    return 40 + (abs(hash((loc, day, hour))) % 140)


async def main() -> None:
    db_path = os.environ.get("DB_PATH", "/data/status.db")
    engine = make_engine(db_path)
    await bootstrap(engine)
    sessionmaker = make_sessionmaker(engine)

    # Повторный запуск демо не должен наслаивать историю.
    async with sessionmaker() as session:
        from sqlalchemy import text
        for table in ("status_check", "status_ru_probe"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    moment = (now - timedelta(days=29)).replace(hour=0, minute=0)
    written = 0
    while moment <= now:
        day = (now - moment).days
        for loc in LOCATIONS:
            ok, total = world_counts(loc, day, moment.hour)
            await store.add_check(sessionmaker, key=loc, source="world",
                                  level="green" if ok else "red",
                                  latency_ms=latency(loc, day, moment.hour) if ok else None,
                                  ok_count=ok, total_count=total, now=moment)
            ok, total = ru_counts(loc, day, moment.hour)
            await store.add_check(sessionmaker, key=loc, source="ru",
                                  level=ru_level(ok, total),
                                  ok_count=ok, total_count=total, now=moment)
            written += 2
        moment += STEP

    await engine.dispose()
    print(f"демо: записано проверок — {written}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
