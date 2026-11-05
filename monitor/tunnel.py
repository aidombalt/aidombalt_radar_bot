"""Мост для Colab без своего домена: Радар сам поднимает туннель Cloudflare (cloudflared quick tunnel),
проверяет его «насквозь» и сообщает адрес в бот. Включается строкой TUNNEL=cloudflared в .env.

Адрес вида https://…trycloudflare.com меняется при каждом перезапуске туннеля — тогда бот присылает новый,
и его нужно вставить в Colab → 🔑 Секреты → RADAR_URL. Снаружи через туннель виден только мост /api/brain/*
(веб-интерфейс закрыт сторожем netguard).

Пилот 24.09: туннель регистрировался, а Colab получал 530. Поэтому адрес рассылается только после того, как
Радар сам сходил по нему на /health и получил ответ; туннель, который перестал пропускать запросы,
перезапускается (с другим протоколом). Полный вывод cloudflared — в data/cloudflared.log."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .settings import DATA_DIR
from .util import html_escape

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger("radar.tunnel")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
PROTOCOLS = ("http2", "quic")
CONNECT_TIMEOUT = 60          # сек до «Registered tunnel connection»
PROBE_FIRST, PROBE_EVERY, PROBE_RETRY = 10, 120, 20
PROBE_FAILS_TO_RESTART = 3
WIN_PATHS = [r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe",
             str(Path.home() / "AppData/Local/Microsoft/WinGet/Links/cloudflared.exe")]


def find_cloudflared() -> str | None:
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    return next((p for p in WIN_PATHS if os.path.exists(p)), None)


class Tunnel:
    LOGNAME = "cloudflared.log"

    def __init__(self, ctx: "Ctx", port: int):
        self.ctx, self.port = ctx, port
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self.pending: str | None = None
        self.fails = 0                      # подряд неудачных запусков (для одного уведомления людям)
        self.last_probe = ""                # для /brain и GUI: «✅ 13:05» / «❌ 530 error code: 1033»
        self._logf = None

    # ------------------------------------------------------------------ жизненный цикл
    async def run(self) -> None:
        exe = find_cloudflared()
        if not exe:
            log.warning("TUNNEL=cloudflared, но cloudflared не найден. Установите: winget install --id Cloudflare.cloudflared")
            self.ctx.db.event("error", "tunnel", "cloudflared не найден — мост для Colab не поднят")
            return
        delay, attempt = 5, 0
        while True:
            # QUIC (UDP 7844) в офисных сетях часто режут — начинаем с http2 (TCP 7844), дальше чередуем
            proto = PROTOCOLS[attempt % len(PROTOCOLS)]
            attempt += 1
            self.pending, probe, self._probe_task = None, None, None
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    exe, "tunnel", "--no-autoupdate", "--protocol", proto, "--url", f"http://127.0.0.1:{self.port}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                log.info("туннель Cloudflare запускается (протокол %s)…", proto)
                verified = asyncio.Event()
                ok = await self._watch(proto, lambda url: self._start_probe(url, proto, verified))
                probe = self._probe_task
                if ok and verified.is_set():
                    delay, self.fails = 5, 0
                else:
                    self._failed(proto)
            except asyncio.CancelledError:
                await self._shutdown()
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("туннель: %s", e)
            finally:
                if probe and not probe.done():
                    probe.cancel()
            self.stop()
            self.ctx.brain.tunnel_url = None
            log.warning("туннель Cloudflare перезапускается через %s с", delay)
            await asyncio.sleep(delay)
            delay = min(300, delay * 2)

    _probe_task: asyncio.Task | None = None

    def _start_probe(self, url: str, proto: str, verified: asyncio.Event) -> None:
        self._probe_task = asyncio.create_task(self._probe_loop(url, proto, verified))

    def _failed(self, proto: str) -> None:
        self.fails += 1
        if self.fails == len(PROTOCOLS):   # оба протокола не прошли — сообщаем людям один раз
            self.ctx.alerts.admin_notice(
                "🔗 <b>Мост для Colab не работает:</b> туннель Cloudflare из сети офиса поднимается, но соединения "
                f"с Cloudflare рвутся ({html_escape(self.last_probe or 'нет соединения')[:200]}). Радар работает на локальной модели. "
                "Надёжное решение — VPS в РФ (TUNNEL=ssh, см. README). Подробности — data/cloudflared.log.", "tunnel-fail")

    async def _shutdown(self) -> None:
        self.stop()
        if self.proc:
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

    # ------------------------------------------------------------------ вывод cloudflared
    async def _watch(self, proto: str, on_registered) -> bool:
        """Читает вывод cloudflared до конца процесса. True — соединение с Cloudflare было установлено."""
        assert self.proc and self.proc.stdout
        registered, last_err, last_warn_at = False, "", 0.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_TIMEOUT
        while True:
            timeout = None if registered else max(0.1, deadline - loop.time())
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            except asyncio.TimeoutError:
                msg = (f"туннель не соединился с Cloudflare за {CONNECT_TIMEOUT} с (протокол {proto}); "
                       f"возможно, сеть не пускает на порт 7844. {last_err[:200]}")
                log.error(msg)
                self.ctx.db.event("error", "tunnel", msg)
                return False
            if not raw:
                return registered
            line = raw.decode("utf-8", "replace").strip()
            self._log_line(line)
            m = URL_RE.search(line)
            if m:
                self.pending = m.group(0)
            elif "Registered tunnel connection" in line:
                if not registered and self.pending:
                    on_registered(self.pending)
                registered = True
            elif " ERR " in line or " WRN " in line or "Unregistered tunnel connection" in line:
                last_err = line.split(" ", 2)[-1]
                if loop.time() - last_warn_at > 20:     # не больше строки в 20 с, полный вывод — в файле
                    last_warn_at = loop.time()
                    log.warning("cloudflared: %s", last_err[:300])

    def _log_line(self, line: str) -> None:
        try:
            path = DATA_DIR / self.LOGNAME
            if self._logf is None or (path.exists() and path.stat().st_size > 2_000_000):
                if self._logf:
                    self._logf.close()
                self._logf = open(path, "w" if path.exists() and path.stat().st_size > 2_000_000 else "a",
                                  encoding="utf-8")
            self._logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
            self._logf.flush()
        except OSError:
            pass

    # ------------------------------------------------------------------ проверка «насквозь»
    async def probe(self, url: str) -> tuple[bool, str]:
        """Сами идём по публичному адресу туннеля на /health — ровно как пойдёт Colab."""
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{url}/health")
            if r.status_code == 200 and '"ok"' in r.text:
                return True, f"✅ {time.strftime('%H:%M')}"
            body = re.sub(r"\s+", " ", r.text)[:80]
            return False, f"❌ HTTP {r.status_code} {body}"
        except Exception as e:  # noqa: BLE001
            return False, f"❌ {type(e).__name__}: {e}"[:120]

    async def _probe_loop(self, url: str, proto: str, verified: asyncio.Event) -> None:
        await asyncio.sleep(PROBE_FIRST)
        fails = 0
        while True:
            ok, self.last_probe = await self.probe(url)
            self.ctx.brain.tunnel_probe = self.last_probe
            if ok:
                if not verified.is_set():
                    verified.set()
                    self.set_url(url)
                fails = 0
                await asyncio.sleep(PROBE_EVERY)
                continue
            fails += 1
            log.warning("мост для Colab не отвечает снаружи (%s, протокол %s), попытка %s/%s",
                        self.last_probe, proto, fails, PROBE_FAILS_TO_RESTART)
            if fails >= PROBE_FAILS_TO_RESTART:
                self.ctx.db.event("error", "tunnel", f"туннель ({proto}) не пропускает запросы: {self.last_probe}")
                self.ctx.brain.tunnel_url = None
                self.stop()                        # _watch увидит конец процесса → перезапуск с другим протоколом
                return
            await asyncio.sleep(PROBE_RETRY)

    def set_url(self, url: str) -> None:
        self.url = self.ctx.brain.tunnel_url = url
        old = self.ctx.db.kv_get("tunnel_url")
        log.info("мост для Colab проверен и работает: %s  (вставьте в Colab → 🔑 Секреты → RADAR_URL)", url)
        if url != old:
            self.ctx.db.kv_set("tunnel_url", url)
            self.ctx.alerts.admin_notice(
                f"🔗 <b>Адрес моста для Colab{' изменился' if old else ''}:</b>\n<code>{url}</code>\n"
                "Colab → 🔑 Секреты → <b>RADAR_URL</b> — вставить, затем «Выполнить всё». "
                "(Пока Colab выключен, ничего делать не нужно — Радар работает на локальной модели.)", f"tunnel:{url}")

    def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass


# ====================================================================== мост через свой VPS (надёжный вариант)
SSH_PATHS = [r"C:\Windows\System32\OpenSSH\ssh.exe"]
KEYGEN_PATHS = [r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"]


def _find(name: str, extra: list[str]) -> str | None:
    return shutil.which(name) or next((p for p in extra if os.path.exists(p)), None)


def public_site(host: str) -> str:
    """1.2.3.4 → 1-2-3-4.sslip.io: бесплатное имя без покупки домена, под него Caddy сам получит сертификат."""
    return f"{host.replace('.', '-')}.sslip.io" if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) else host


class SshTunnel(Tunnel):
    """TUNNEL=ssh: ноутбук держит исходящее SSH-соединение к VPS в РФ (обычный трафик внутри страны — его не режут),
    VPS публикует мост по HTTPS с постоянным адресом. Colab ходит на VPS. Секрет RADAR_URL меняется один раз.
      .env:  TUNNEL=ssh   TUNNEL_HOST=1.2.3.4   (необязательно: TUNNEL_USER=radar  TUNNEL_REMOTE_PORT=18080)
    При первом запуске Радар создаёт ключ и файл data/vps_setup_ready.sh — его выполняют на VPS один раз."""
    LOGNAME = "ssh_tunnel.log"

    def __init__(self, ctx: "Ctx", port: int):
        super().__init__(ctx, port)
        self.host = os.environ.get("TUNNEL_HOST", "").strip()
        self.user = os.environ.get("TUNNEL_USER", "radar").strip() or "radar"
        self.rport = int(os.environ.get("TUNNEL_REMOTE_PORT", "18080") or 18080)
        self.key = DATA_DIR / "tunnel_ed25519"
        self.site = public_site(self.host) if self.host else ""

    def ensure_key(self) -> str | None:
        """Ключ для туннеля (без пароля, только для этого моста). Возвращает публичную часть."""
        pub = self.key.with_suffix(".pub")
        if not self.key.exists():
            kg = _find("ssh-keygen", KEYGEN_PATHS)
            if not kg:
                return None
            import subprocess

            subprocess.run([kg, "-t", "ed25519", "-N", "", "-C", "radar-bridge", "-f", str(self.key)],
                           check=True, capture_output=True)
            if os.name == "nt":   # OpenSSH для Windows не принимает ключ, который могут читать другие пользователи
                user = os.environ.get("USERNAME", "")
                subprocess.run(["icacls", str(self.key), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True)
        return pub.read_text(encoding="utf-8").strip() if pub.exists() else None

    def write_setup(self, pubkey: str) -> Path:
        tpl = (Path(__file__).resolve().parent.parent / "deploy" / "vps_setup.sh").read_text(encoding="utf-8")
        out = DATA_DIR / "vps_setup_ready.sh"
        out.write_text(tpl.replace("__PUBKEY__", pubkey).replace("__SITE__", self.site)
                       .replace("__PORT__", str(self.rport)).replace("__USER__", self.user), encoding="utf-8", newline="\n")
        return out

    async def run(self) -> None:
        ssh = _find("ssh", SSH_PATHS)
        if not self.host or not ssh:
            msg = "TUNNEL=ssh: " + ("не задан TUNNEL_HOST (IP вашего VPS)" if not self.host else
                                    "нет ssh.exe — Параметры → Приложения → Дополнительные компоненты → «Клиент OpenSSH»")
            log.warning(msg)
            self.ctx.db.event("error", "tunnel", msg)
            return
        pub = self.ensure_key()
        if not pub:
            log.warning("TUNNEL=ssh: не удалось создать ключ (нет ssh-keygen)")
            return
        setup = self.write_setup(pub)
        url = f"https://{self.site}"
        log.info("мост через VPS: %s → %s (скрипт для VPS: %s)", self.host, url, setup)
        delay, told_key = 5, False
        while True:
            verified, probe = asyncio.Event(), None
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    ssh, "-N", "-T", "-i", str(self.key), "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
                    "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={DATA_DIR / 'known_hosts'}",
                    "-R", f"127.0.0.1:{self.rport}:127.0.0.1:{self.port}", f"{self.user}@{self.host}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                probe = asyncio.create_task(self._probe_loop(url, "ssh", verified))
                assert self.proc.stdout
                async for raw in self.proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    self._log_line(line)
                    if line:
                        log.warning("ssh: %s", line[:300])
                    if "Permission denied" in line and not told_key:
                        told_key = True
                        self.ctx.alerts.admin_notice(
                            "🔗 <b>Мост через VPS:</b> VPS не принимает ключ ноутбука — скрипт настройки ещё не выполнен. "
                            f"Откройте на ноутбуке <code>{setup}</code>, скопируйте целиком и вставьте в консоль VPS "
                            "(от root). Это делается один раз.", "tunnel-sshkey")
                await self.proc.wait()
                if verified.is_set():
                    delay = 5
            except asyncio.CancelledError:
                if probe:
                    probe.cancel()
                await self._shutdown()
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("ssh-туннель: %s", e)
            finally:
                if probe and not probe.done():
                    probe.cancel()
            self.ctx.brain.tunnel_url = None
            log.warning("ssh-туннель к VPS оборвался — переподключение через %s с", delay)
            await asyncio.sleep(delay)
            delay = min(120, delay * 2)


def make_tunnel(ctx: "Ctx", port: int) -> Tunnel | None:
    mode = os.environ.get("TUNNEL", "").strip().lower()
    return SshTunnel(ctx, port) if mode == "ssh" else Tunnel(ctx, port) if mode == "cloudflared" else None
