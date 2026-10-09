"""Настройки сервиса: только переменные окружения, файлов настроек нет.

Значения по умолчанию рассчитаны на ``docker-compose.yml`` из этого репозитория:
сервис ``probe`` видит контейнер ``xray-checker`` по имени ``xray-checker``.

Токен Globalping и пароль xray-checker в лог не пишутся.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in TRUE_VALUES


def _env_tags(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    #: xray-checker: базовый URL внутри docker-сети. Пусто — источник «из мира» выключен
    #: (и локации придётся задать через ``TARGETS``).
    checker_url: str = ""
    checker_user: str = ""
    checker_password: str = ""

    #: Токен Globalping (Bearer). Пусто — источник «из России» выключен.
    globalping_token: str = ""
    #: База API Globalping; меняется только для тестов/прокси.
    globalping_base: str = "https://api.globalping.io"
    #: Страна зондов и их теги (``eyeball-network`` — домашние провайдеры).
    ru_country: str = "RU"
    ru_tags: tuple[str, ...] = ("eyeball-network",)
    #: Сколько зондов в одном измерении (1 зонд = 1 тест квоты).
    ru_probes: int = 20

    #: Список целей вручную: ``Имя=host:port`` через запятую. Пусто — локации
    #: собираются из подписки xray-checker.
    targets: tuple[str, ...] = ()

    world_interval: int = 300
    ru_interval: int = 900
    #: Файл SQLite. В compose — том, иначе история теряется при пересоздании.
    db_path: str = "data/status.db"
    #: Показывать адреса и порты в публичном API. По умолчанию нет: публичная
    #: страница не должна превращаться в список целей для блокировки.
    expose_targets: bool = False
    cache_ttl: int = 60
    tick: float = 15.0
    #: Слушать на всех интерфейсах внутри контейнера.
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            checker_url=os.environ.get("CHECKER_URL", "http://xray-checker:2112").strip(),
            checker_user=os.environ.get("CHECKER_USER", "").strip(),
            checker_password=os.environ.get("CHECKER_PASSWORD", "").strip(),
            globalping_token=os.environ.get("GLOBALPING_TOKEN", "").strip(),
            globalping_base=os.environ.get(
                "GLOBALPING_BASE", "https://api.globalping.io"
            ).strip(),
            ru_country=os.environ.get("RU_COUNTRY", "RU").strip() or "RU",
            ru_tags=_env_tags("RU_TAGS", ("eyeball-network",)),
            ru_probes=_env_int("RU_PROBES", 20),
            targets=tuple(_env_list("TARGETS")),
            world_interval=_env_int("WORLD_INTERVAL", 300),
            ru_interval=_env_int("RU_INTERVAL", 900),
            db_path=os.environ.get("DB_PATH", "data/status.db").strip(),
            expose_targets=_env_bool("EXPOSE_TARGETS", False),
            cache_ttl=_env_int("CACHE_TTL", 60),
            tick=float(_env_int("TICK", 15)),
            host=os.environ.get("HOST", "0.0.0.0").strip(),
            port=_env_int("PORT", 8080),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )

    @property
    def sources_note(self) -> str:
        """Какие источники включены — для строки в логе при старте (без секретов)."""

        world = "да" if self.checker_url else "нет"
        ru = "да" if self.globalping_token else "нет"
        manual = f"{len(self.targets)} шт." if self.targets else "нет"
        return f"из мира={world}, из России={ru}, свои цели={manual}"
