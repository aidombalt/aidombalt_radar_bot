"""Мелкие общие утилиты: время, хэши, HTTP-клиент, логирование."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("radar")

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 BaltDomRadar/1.0"
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def from_ts(ts: int | float) -> str:
    return iso(datetime.fromtimestamp(ts, UTC))


def msk(s: str | datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    dt = parse_iso(s) if isinstance(s, str) else s
    return dt.astimezone(MSK).strftime(fmt) if dt else "—"


def ago(minutes: float = 0, hours: float = 0, days: float = 0) -> str:
    return iso(now_utc() - timedelta(minutes=minutes, hours=hours, days=days))


def sha1(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def html_escape(s: str | None) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ws = re.compile(r"\s+")


def squash(s: str | None) -> str:
    return _ws.sub(" ", s or "").strip()


# Хосты, закрытые в РФ: при заданном RELAY_URL запросы к ним идут через свой релей на Cloudflare Workers
RELAY_HOSTS = {"api.telegram.org", "t.me", "telegram.me", "news.google.com", "hc-ping.com"}


def relay_url() -> str:
    """RELAY_URL=https://radar-relay.<имя>.workers.dev/<ключ>  (deploy/cloudflare_worker.js)."""
    return os.environ.get("RELAY_URL", "").strip().rstrip("/")


class RelayTransport(httpx.AsyncBaseTransport):
    """https://t.me/s/x → https://<релей>/<ключ>/t.me/s/x. Остальные хосты (ВК, госсайты) — напрямую."""

    def __init__(self, relay: str, inner: httpx.AsyncBaseTransport):
        self.base, self.inner = httpx.URL(relay), inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in RELAY_HOSTS:
            raw = self.base.raw_path.rstrip(b"/") + b"/" + request.url.host.encode() + request.url.raw_path
            request.url = self.base.copy_with(raw_path=raw)
            request.headers["Host"] = self.base.netloc.decode()
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_http(proxy: str | None = None, timeout: float = 25.0, relay: str | None = None) -> httpx.AsyncClient:
    """Один HTTP-клиент на компонент: таймауты, ретраи соединения, UA, опц. прокси или релей."""
    relay = relay_url() if relay is None else relay
    transport: httpx.AsyncBaseTransport
    if relay and not proxy:
        # Через релей: новое соединение на каждый запрос. Долгоживущие соединения к Cloudflare из РФ
        # периодически «замирают» — не даём одному зависшему соединению тормозить все следующие запросы.
        inner = httpx.AsyncHTTPTransport(retries=2, limits=httpx.Limits(max_keepalive_connections=0))
        transport = RelayTransport(relay, inner)
        timeout = max(timeout, 45.0)   # через релей дольше: ноутбук → Cloudflare → сайт → обратно
    else:
        transport = httpx.AsyncHTTPTransport(retries=2, proxy=proxy or None)
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        follow_redirects=True,
    )


async def backoff_sleep(attempt: int, base: float = 1.5, cap: float = 60.0) -> None:
    await asyncio.sleep(min(cap, base * (2 ** attempt)) * (0.5 + random.random()))


def setup_logging(level: str = "INFO", path: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if path:
        from logging.handlers import RotatingFileHandler

        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "aiogram.event", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Windows: «WinError 10054 … _call_connection_lost» — сервер закрыл соединение, данные уже получены. Не ошибка.
    logging.getLogger("asyncio").addFilter(
        lambda r: not ("_call_connection_lost" in r.getMessage() or "10054" in str(r.exc_info[1] if r.exc_info else "")))
