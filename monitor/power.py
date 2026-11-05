"""Выключение и перезапуск Радара из веб-интерфейса; штатный выход по Ctrl+C (сборка 2026.10.23).

Коды выхода процесса — общий язык с тем, кто Радар запускает:
  0  — штатная остановка (кнопка «Выключить», Ctrl+C, systemctl stop): НЕ перезапускать;
  75 — «Перезапустить» из GUI: запустить снова сразу;
  остальное — сбой (1, 3 — не поднялся веб-сервер): перезапустить через паузу.
start_radar.bat, deploy/radar.service (Restart=on-failure) и cron-сторож (deploy/radar-watchdog.sh) это понимают.
Остановка из GUI кладёт метку data/stopped_by_user: внешний сторож её видит и не поднимает Радар обратно; при следующем
запуске метка снимается, а в служебном сообщении о перерыве будет сказано, кто и когда выключил.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from .settings import DATA_DIR
from .util import iso, msk

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger("radar")

EXIT_STOP = 0
EXIT_RESTART = 75
STOP_MARKER = DATA_DIR / "stopped_by_user"
LAST_STOP_KEY = "last_stop"


def supervisor() -> str:
    """Кто запускает Радар: bat (start_radar.bat) | systemd | docker | '' (вручную из консоли)."""
    if os.environ.get("RADAR_SUPERVISOR", "").lower() == "docker":
        return "docker"
    if os.environ.get("RADAR_LAUNCHER", "").lower() == "bat":
        return "bat"
    if os.environ.get("INVOCATION_ID"):            # systemd выставляет каждому запуску службы
        return "systemd"
    return ""


def can_restart() -> bool:
    """Перезапуск возможен, только если есть кому запустить процесс снова."""
    return supervisor() in ("bat", "systemd", "docker")


def can_stop() -> tuple[bool, str]:
    """Docker с restart: unless-stopped поднимет контейнер сам — там выключают командой, а не кнопкой."""
    if supervisor() == "docker":
        return False, "Радар работает в Docker: выключение — `docker compose stop radar` на сервере"
    return True, ""


def request(ctx: "Ctx", action: str, who: str = "GUI") -> int:
    """Записать намерение и предупредить команду. Сам выход делает вызывающий (app.shutdown())."""
    code = EXIT_RESTART if action == "restart" else EXIT_STOP
    ctx.exit_code = code
    stamp = msk(iso(), "%d.%m %H:%M")
    if code == EXIT_STOP:
        try:
            STOP_MARKER.write_text(f"{iso()} {who}\n", "utf-8")
        except OSError:
            log.warning("не удалось записать метку остановки %s", STOP_MARKER)
        ctx.db.kv_set(LAST_STOP_KEY, {"at": iso(), "by": who, "action": "stop"})
        text = (f"⏹ Радар выключен из веб-интерфейса ({who}, {stamp}). Пока его не запустят снова, публикации не "
                "собираются и алерты не приходят. Запуск: start_radar.bat на ноутбуке (или systemctl start radar).")
    else:
        ctx.db.kv_set(LAST_STOP_KEY, {"at": iso(), "by": who, "action": "restart"})
        text = f"🔄 Радар перезапускается из веб-интерфейса ({who}, {stamp}) — вернётся через минуту."
    ctx.db.event("info", "app", ("Выключение" if code == EXIT_STOP else "Перезапуск") + f" из веб-интерфейса ({who})")
    log.info("%s по команде из веб-интерфейса (%s)", "выключение" if code == EXIT_STOP else "перезапуск", who)
    if code == EXIT_STOP:
        ctx.alerts.admin_notice(text, f"power:{iso()}")
    return code


async def flush(ctx: "Ctx", timeout: float = 8.0) -> None:
    """Успеть отправить служебное «Радар выключен», не задерживая выход дольше timeout."""
    try:
        await asyncio.wait_for(ctx.alerts.process(), timeout)
    except Exception as ex:  # noqa: BLE001 — сеть не должна мешать выключению
        log.info("служебное сообщение о выключении не ушло сразу (%s) — уйдёт после запуска", type(ex).__name__)


def on_start(ctx: "Ctx") -> dict | None:
    """При запуске: снять метку остановки; вернуть {at, by, action} последней остановки из GUI (для сообщения о перерыве)."""
    last = ctx.db.kv_get(LAST_STOP_KEY)
    if STOP_MARKER.exists():
        try:
            STOP_MARKER.unlink()
        except OSError:
            log.warning("не удалось снять метку остановки %s", STOP_MARKER)
    if last:
        ctx.db.x("DELETE FROM kv WHERE key=?", (LAST_STOP_KEY,))
    return last
