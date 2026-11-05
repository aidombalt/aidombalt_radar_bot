"""MAX Bot API. Direct HTTPS only; no Telegram relay, ambient proxy or token in URLs."""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class MaxError(RuntimeError):
    def __init__(self, status: int, code: str = "unknown", retry_after: float = 0):
        # Never put remote message/body/URL in logs: those can reflect a secret.
        self.status, self.code, self.retry_after = status, code[:80], retry_after
        super().__init__(f"MAX HTTP {status} ({self.code})")

    @property
    def denied(self):
        return self.status == 401 or (self.status == 403 and self.code in ("chat.denied", "access.denied", "forbidden"))

    @property
    def temporary(self):
        return self.status in (0, 408, 429) or self.status >= 500 or self.code == "attachment.not.ready"


def network_error_code(error):
    """Classify locally; never copy a URL, token or server response into a log."""
    if isinstance(error, httpx.TimeoutException):
        return "network.timeout"
    seen = set()
    cause = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(cause):
            return "tls.verify.failed"
        if isinstance(cause, socket.gaierror):
            return "network.dns.failed"
        cause = cause.__cause__ or cause.__context__
    return "network.error"


def error_hint(error):
    hints = {
        "tls.verify.failed": "не доверен HTTPS-сертификат; настройте доверенный УЦ в Windows или max.ca_bundle (PEM)",
        "network.dns.failed": "не удалось найти адрес сервера Max; проверьте DNS и доступ в интернет",
        "network.timeout": "сервер Max не ответил вовремя; проверьте подключение",
        "network.error": "нет прямого HTTPS-соединения с Max; проверьте сеть и межсетевой экран",
    }
    if error.status == 401:
        return "Max отклонил токен; проверьте MAX_BOT_TOKEN и перезапустите Радар"
    if error.status == 403:
        return "проверьте доступ бота к чату и его права"
    return hints.get(error.code, "проверьте статус Max и настройки подключения")


def retry_seconds(value: str) -> float:
    try:
        return max(1, min(86400, float(value)))
    except (ValueError, TypeError):
        try:
            return max(1, min(86400, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 1


class MaxAPI:
    def __init__(self, token: str, base_url="https://platform-api2.max.ru", ca_bundle="", transport=None):
        url = urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("MAX api_url должен быть HTTPS-адресом без учётных данных")
        self._token = token
        verify = ssl.create_default_context()
        if ca_bundle:
            verify.load_verify_locations(cafile=ca_bundle)
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), trust_env=False, verify=verify,
                                      timeout=20, follow_redirects=False, transport=transport)
        self._gate = asyncio.Lock()
        self._next = 0.0
        self._blocked_until = 0.0
        self._answer_gate = asyncio.Lock()
        self._answer_next = 0.0

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, *, params=None, json=None, timeout=20):
        if self._blocked_until > time.monotonic():
            raise MaxError(429, "rate.limit", self._blocked_until - time.monotonic())
        async with self._gate:
            await asyncio.sleep(max(0, self._next - time.monotonic()))
            self._next = time.monotonic() + .04  # at most 25 rps, polls have their own 300ms spacing
        try:
            res = await self.http.request(method, path, params=params, json=json, timeout=timeout,
                                          headers={"Authorization": self._token})
        except httpx.HTTPError as err:
            raise MaxError(0, network_error_code(err)) from None
        try:
            body = res.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            raise MaxError(res.status_code, "invalid.response")
        if res.is_error or body.get("success") is False:
            code = str(body.get("code") or "request.failed")
            # Do not permit a server-reflected token or arbitrary prose in error codes.
            if not all(c.isalnum() or c in "._-" for c in code) or self._token in code:
                code = "request.failed"
            retry = retry_seconds(res.headers.get("Retry-After", "1")) if res.status_code == 429 else 0
            if retry:
                self._blocked_until = time.monotonic() + retry
            raise MaxError(res.status_code, code, retry)
        if res.status_code >= 300:
            raise MaxError(res.status_code, "redirect.denied")
        return body

    async def me(self):
        return await self.request("GET", "/me")

    async def send(self, body, *, chat_id=None, user_id=None):
        if (chat_id is None) == (user_id is None):
            raise ValueError("Нужен ровно один адресат MAX")
        params = {"chat_id": int(chat_id)} if chat_id is not None else {"user_id": int(user_id)}
        return await self.request("POST", "/messages", params=params, json=body)

    async def edit(self, mid, body):
        return await self.request("PUT", "/messages", params={"message_id": mid}, json=body)

    async def delete(self, mid):
        return await self.request("DELETE", "/messages", params={"message_id": mid})

    async def answer(self, callback_id, notification="", message=None):
        body = {"notification": notification[:250]}
        if message is not None:
            body["message"] = message
        # MAX limits callback answers to 2/s per dialog; a global gate is conservative.
        async with self._answer_gate:
            await asyncio.sleep(max(0, self._answer_next-time.monotonic()))
            self._answer_next = time.monotonic() + .5
            return await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)

    async def updates(self, marker=None, timeout=30, types=None):
        params = {"timeout": min(90, timeout), "limit": 100}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        return await self.request("GET", "/updates", params=params, timeout=timeout + 10)

    async def subscriptions(self):
        return await self.request("GET", "/subscriptions")

    async def subscribe(self, url, secret, update_types):
        return await self.request("POST", "/subscriptions", json={"url": url, "secret": secret, "update_types": update_types})

    async def unsubscribe(self, url):
        return await self.request("DELETE", "/subscriptions", params={"url": url})

    async def chat(self, chat_id):
        return await self.request("GET", f"/chats/{int(chat_id)}")

    async def member(self, chat_id):
        return await self.request("GET", f"/chats/{int(chat_id)}/members/me")

    async def admins(self, chat_id):
        return await self.request("GET", f"/chats/{int(chat_id)}/members/admins")

    async def set_commands(self, commands):
        return await self.request("PATCH", "/me/commands", json={"commands": commands})

    async def pin(self, chat_id, mid):
        return await self.request("PUT", f"/chats/{int(chat_id)}/pin", json={"message_id": mid, "notify": False})

    async def unpin(self, chat_id):
        return await self.request("DELETE", f"/chats/{int(chat_id)}/pin")

    async def pinned(self, chat_id):
        return await self.request("GET", f"/chats/{int(chat_id)}/pin")

    async def upload(self, path):
        info = await self.request("POST", "/uploads", params={"type": "file"})
        url = info.get("url", "")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise MaxError(0, "unsafe.upload.url")
        # API token belongs only to platform-api. Signed upload URL carries its own authorization.
        try:
            with Path(path).open("rb") as file:
                res = await self.http.post(url, files={"data": (Path(path).name, file)}, timeout=90)
            res.raise_for_status()
            uploaded = res.json()
        except (httpx.HTTPError, ValueError):
            raise MaxError(0, "upload.failed") from None
        token = uploaded.get("token") or info.get("token")
        if not token:
            raise MaxError(0, "upload.no.token")
        return {"type": "file", "payload": {"token": token}}
