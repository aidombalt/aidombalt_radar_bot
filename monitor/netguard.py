"""Сторож входа: из интернета (через туннель/прокси) открыт только мост для Colab и /health.
Веб-интерфейс и бот-API — только из локальной сети. Так любой туннель (cloudflared, ssh -R, VPS) безопасен
по умолчанию, даже если пароль GUI простой. Отключается настройкой network.gui_public=true."""
from __future__ import annotations

import ipaddress
from typing import Callable

PUBLIC_PREFIXES = ("/api/brain/", "/health")
FWD_HEADERS = (b"cf-connecting-ip", b"x-real-ip", b"x-forwarded-for", b"true-client-ip")
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10")]


def is_local(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip().strip("[]").split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr in n for n in LOCAL_NETS)


def client_chain(scope: dict) -> list[str]:
    """Все адреса, через которые пришёл запрос: сокет + заголовки прокси/туннеля (подделать можно только
    добавив адрес — реальный внешний адрес туннель всё равно допишет, и он не локальный)."""
    ips = [scope["client"][0]] if scope.get("client") else []
    for k, v in scope.get("headers", []):
        if k in FWD_HEADERS:
            ips += [p for p in v.decode("latin-1").split(",") if p.strip()]
        elif k == b"forwarded":
            for part in v.decode("latin-1").replace(",", ";").split(";"):
                if part.strip().lower().startswith("for="):
                    ips.append(part.split("=", 1)[1].strip('" '))
    return ips


class LocalOnly:
    def __init__(self, app, allow_public: Callable[[], bool] = lambda: False):
        self.app, self.allow_public = app, allow_public

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not self.allowed(scope):
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body",
                            "body": "403: веб-интерфейс Радара открыт только из локальной сети".encode()})
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

    def allowed(self, scope: dict) -> bool:
        if scope.get("path", "") == "/api/max/webhook" or scope.get("path", "").startswith(PUBLIC_PREFIXES) or self.allow_public():
            return True
        chain = client_chain(scope)
        return bool(chain) and all(is_local(ip) for ip in chain)
