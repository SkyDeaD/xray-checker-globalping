"""Заглушки для демо: панель Remnawave + xray-checker + Globalping на одном порту.

Только stdlib. Нужен, чтобы посмотреть страницу, не подключая свои серверы:

  docker compose -f docker-compose.demo.yml up -d      # http://localhost:8090

Отвечает на те же запросы, что настоящие сервисы:
  GET  /api/hosts, /api/nodes, /api/internal-squads   — панель Remnawave;
  GET  /api/v1/proxies                                — xray-checker;
  POST /v1/measurements, GET /v1/measurements/<id>    — Globalping.

Все адреса — example.com, провайдеры и ASN условные: данных реальных серверов тут нет.
"""

from __future__ import annotations

import contextlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 2199

#: Цель → сколько российских зондов из 20 её видят. Четыре разных случая для наглядности.
RU_OK = {
    "nl.example.com": 20,      # доступен всем
    "wl.example.com": 18,      # белые списки: почти всем
    "pl.example.com": 15,      # блокирует крупный провайдер
    "de.example.com": 4,       # из России практически закрыт
}

HOSTS = [
    {"address": "nl.example.com", "port": 443, "nodes": ["n-nl"], "isDisabled": False,
     "viewPosition": 1, "remark": "Нидерланды", "inbound": {"configProfileInboundUuid": "i-nl"}},
    {"address": "wl.example.com", "port": 443, "nodes": ["n-nl"], "isDisabled": False,
     "viewPosition": 2, "remark": "Нидерланды · белые списки",
     "inbound": {"configProfileInboundUuid": "i-wl"}},
    {"address": "pl.example.com", "port": 8443, "nodes": ["n-pl"], "isDisabled": False,
     "viewPosition": 3, "remark": "Польша", "inbound": {"configProfileInboundUuid": "i-pl"}},
    {"address": "de.example.com", "port": 2087, "nodes": ["n-de"], "isDisabled": False,
     "viewPosition": 4, "remark": "Германия", "inbound": {"configProfileInboundUuid": "i-de"}},
    # Служебный хост-заглушка из панели: нод нет, в локации не попадёт.
    {"address": "example.com", "port": 443, "nodes": [], "isDisabled": False,
     "viewPosition": 5, "remark": "Автовыбор", "inbound": {"configProfileInboundUuid": "i-nl"}},
]

NODES = {"nodes": [
    {"uuid": "n-nl", "name": "NL-1", "countryCode": "NL", "port": 2222, "isDisabled": False,
     "configProfile": {"activeInbounds": [{"uuid": "i-nl", "type": "vless"},
                                          {"uuid": "i-wl", "type": "vless"}]}},
    {"uuid": "n-pl", "name": "PL-1", "countryCode": "PL", "port": 2222, "isDisabled": False,
     "configProfile": {"activeInbounds": [{"uuid": "i-pl", "type": "vless"}]}},
    {"uuid": "n-de", "name": "DE-1", "countryCode": "DE", "port": 2222, "isDisabled": False,
     "configProfile": {"activeInbounds": [{"uuid": "i-de", "type": "vless"}]}},
]}

SQUADS = {"internalSquads": [
    {"uuid": "demo-wl-squad", "name": "Whitelist", "inbounds": [{"uuid": "i-wl"}]},
]}

#: Что видит xray-checker: тот же набор адресов с теми же портами.
PROXIES = {"success": True, "data": [
    {"stableId": "nl", "name": "NL-1", "server": "nl.example.com", "port": 443,
     "online": True, "latencyMs": 123, "groupName": "Нидерланды"},
    {"stableId": "wl", "name": "NL-2", "server": "wl.example.com", "port": 443,
     "online": True, "latencyMs": 131, "groupName": "Нидерланды"},
    {"stableId": "pl", "name": "PL-1", "server": "pl.example.com", "port": 8443,
     "online": True, "latencyMs": 141, "groupName": "Польша"},
    {"stableId": "de", "name": "DE-1", "server": "de.example.com", "port": 2087,
     "online": True, "latencyMs": 168, "groupName": "Германия"},
]}

CITIES = ["Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань",
          "Краснодар", "Омск", "Самара", "Ростов-на-Дону", "Уфа",
          "Пермь", "Воронеж", "Волгоград", "Красноярск", "Тюмень",
          "Саратов", "Тольятти", "Ижевск", "Барнаул", "Ульяновск"]
ISPS = ["Rostelecom", "MTS", "Beeline", "MegaFon", "ER-Telecom", "TTK",
        "Dom.ru", "Sberbank-Telecom", "VimpelCom", "Iskratelecom",
        "UG-Telecom", "TTK-Ural", "MTS-Ural", "Sibirtelecom", "Zelenaya Tochka",
        "MTS-Volga", "Rostelecom-Ural", "Beeline-Sibir", "MegaFon-South", "ER-Telecom-2"]


def measurement(target: str) -> dict:
    ok_n = RU_OK.get(target, 20)
    return {"id": "demo", "status": "finished", "results": [
        {"probe": {"city": CITIES[i], "network": ISPS[i], "asn": 1000 + i, "country": "RU"},
         "result": {"status": "finished",
                    "stats": {"avg": (28.0 + i * 4) if i < ok_n else None,
                              "loss": 0 if i < ok_n else 100,
                              "rcv": 3 if i < ok_n else 0, "total": 3}}}
        for i in range(20)]}


class Handler(BaseHTTPRequestHandler):
    last_target = "nl.example.com"

    def log_message(self, *args):  # тишина в логе
        return

    def _json(self, code: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        with contextlib.suppress(ValueError):        # тело может быть не JSON — не падаем
            Handler.last_target = json.loads(raw or b"{}").get("target") or Handler.last_target
        self._json(202, {"id": "demo", "probesCount": 20})

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/hosts":
            self._json(200, {"response": HOSTS})
        elif path == "/api/nodes":
            self._json(200, {"response": NODES})
        elif path == "/api/internal-squads":
            self._json(200, {"response": SQUADS})
        elif path == "/api/v1/proxies":
            self._json(200, PROXIES)
        elif re.fullmatch(r"/v1/measurements/[^/]+", path):
            self._json(200, measurement(Handler.last_target))
        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"демо-заглушки слушают http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
