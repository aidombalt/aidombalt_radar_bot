"""Точка входа сервера: GUI + API моста для Colab + бот + планировщик в одном процессе."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
from pathlib import Path

from .settings import DATA_DIR, ROOT, _load_dotenv
from .util import setup_logging

log = logging.getLogger("radar")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Радар упоминаний ГК «Балтийский Дом»")
    ap.add_argument("--no-gui", action="store_true", help="без веб-интерфейса (только API моста, бот и планировщик)")
    ap.add_argument("--once", choices=["hot", "warm", "cold", "all"], help="один цикл сбора и выход")
    ap.add_argument("--report", type=float, metavar="ЧАСОВ", help="сформировать отчёт за N часов и выйти")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--case-audit", type=int, metavar="ДНЕЙ",
                    help="выгрузить в Excel пары публикаций, которые Радар считает «той же историей», и выйти")
    ap.add_argument("--brain-test", action="store_true",
                    help="прогнать 10 эталонных публикаций через локальную модель (скорость и качество) и выйти")
    ap.add_argument("--netcheck", action="store_true",
                    help="проверить, что доступно из этой сети (ВК, Telegram, Google, госсайты, почта, Ollama), и выйти")
    ap.add_argument("--maxcheck", action="store_true", help="проверить токен, HTTPS и права Max без отправки сообщений")
    args = ap.parse_args(argv)

    _load_dotenv(Path(os.environ.get("RADAR_ENV_FILE", str(ROOT / ".env"))))  # LOG_LEVEL is also configured in .env.
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), str(DATA_DIR / "radar.log"))
    from . import VERSION

    log.info("Радар v%s", VERSION)
    from .context import Ctx
    from .service import Service

    ctx = Ctx()
    ctx.exit_code = None                 # «Выключить»/«Перезапустить» из GUI (monitor/power.py)
    if args.maxcheck:
        from .alerts.max_check import check

        raise SystemExit(0 if asyncio.run(check(ctx)) else 1)
    if args.case_audit:
        from .cases import audit

        print("файл:", audit(ctx, args.case_audit))
        return
    if args.brain_test or args.netcheck:
        from . import checks

        asyncio.run(checks.brain_test(ctx) if args.brain_test else checks.netcheck(ctx))
        return
    n = ctx.sync_sources_from_yaml()
    from . import VERSION as _v
    if ctx.db.kv_get("rematched_for") != _v:   # новая версия сопоставителя — пересобрать совпадения за 3 дня
        try:
            log.info("пересобраны совпадения за 3 дня по новым правилам: %s", ctx.pipeline.rematch_recent())
        except Exception:  # обслуживание не должно мешать запуску
            log.exception("пересборка совпадений не удалась — Радар работает дальше")
        ctx.db.kv_set("rematched_for", _v)
    if ctx.db.kv_get("compacted_for") != _v:      # новая версия: разовая чистка и сжатие, не дожидаясь ночи
        try:
            size0 = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
            log.info("чистка данных и сжатие базы (разово после обновления, до минуты)…")
            st = ctx.db.compact(vacuum=True)
            ctx.db.backup()
            size1 = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
            log.info("папка data: %.0f МБ → %.0f МБ (шум без текста: %s, удалено: %s)", size0 / 1e6, size1 / 1e6,
                     st["stripped"], st["deleted"])
        except Exception:  # noqa: BLE001 — обслуживание не должно мешать запуску
            log.exception("разовая чистка не удалась — повторится ночью")
        ctx.db.kv_set("compacted_for", _v)
    # Google News резал запросы (503) — это не поломка источников: снимаем красные метки, дальше работает пауза
    ctx.db.x("UPDATE sources SET consecutive_failures=0, breaker_until=NULL, last_error=NULL "
             "WHERE (kind='gnews' OR meta_json LIKE '%via_gnews%') AND last_error LIKE '%HTTP 503%'")
    try:                                           # кандидаты без доказательств (старая схема автопоиска) — скрыть
        from . import candidates
        rv = candidates.review(ctx)
        if rv["hidden"] or rv["dup"]:
            log.info("кандидаты в источники пересмотрены: оставлено %s, скрыто без доказательств %s, дублей %s",
                     rv["kept"], rv["hidden"], rv["dup"])
    except Exception:  # noqa: BLE001
        log.exception("пересмотр кандидатов не удался — Радар работает дальше")
    recapped = ctx.pipeline.recap_recent()
    if recapped:
        log.info("по новым правилам понижено срочных публикаций: %s (нет наших объектов или тема не про работу УК)", len(recapped))
    service = Service(ctx)
    ctx.max_bot.service = service
    if not (args.once or args.report):
        from . import power

        service.last_stop = power.on_start(ctx)        # метку «выключен из GUI» снимаем: Радар снова работает
    service.register_virtual_sources()
    log.info("источников в реестре: %s; объектов: %s адресов", n, len(ctx.reg.addresses))
    for name, ok, hint in (("GUI_PASSWORD", ctx.settings.gui_password, "веб-интерфейс открыт без пароля"),
                           ("BRIDGE_TOKEN", ctx.settings.bridge_token, "Colab не сможет подключиться"),
                           ("VK_SERVICE_TOKEN", ctx.settings.vk_service_token, "ВКонтакте не опрашивается"),
                           ("TG_BOT_TOKEN", ctx.settings.tg_bot_token, "бот выключен, алерты только почтой")):
        if not ok:
            log.warning("⚠️  %s не задан в .env — %s", name, hint)

    if args.once or args.report:
        async def one():
            if args.once:
                print(await service.collect(args.once))
                print("разобрано:", await service.router.tick())
                await ctx.alerts.process()
            if args.report:
                from .reports import builder

                print(await builder.build(ctx, hours=args.report, kind="manual", send=False, router=service.router))
        asyncio.run(one())
        return

    from nicegui import app, ui

    from .alerts.bot import run_bot
    from .brain.bridge import make_router

    app.include_router(make_router(ctx))
    from .alerts.max_bot import make_router as make_max_router
    app.include_router(make_max_router(ctx))
    from .netguard import LocalOnly   # через туннель/прокси снаружи виден только мост для Colab

    app.add_middleware(LocalOnly, allow_public=lambda: bool(ctx.settings.network.gui_public))
    if not args.no_gui:
        from .gui import setup_gui

        setup_gui(ctx, service)
    else:
        @ui.page("/")
        def _root():
            ui.label("Радар работает в режиме без GUI.")

    tasks: list[asyncio.Task] = []
    from .tunnel import make_tunnel   # мост для Colab: TUNNEL=ssh (свой VPS) или TUNNEL=cloudflared

    tunnel = make_tunnel(ctx, args.port or ctx.settings.gui_port)

    async def startup():
        if tunnel:
            tasks.append(asyncio.create_task(tunnel.run()))
        tasks.append(asyncio.create_task(service.run_forever()))
        tasks.append(asyncio.create_task(run_bot(ctx, service)))
        tasks.append(asyncio.create_task(ctx.max_bot.run()))
        for iid in dict.fromkeys(recapped):   # их карточки в Telegram сворачиваются в «снято автоматически»
            ctx.alerts.schedule_refresh(iid, "dropped", "по новым правилам: не наш объект или та же история")
        ctx.db.event("info", "app", "Радар запущен")

    async def shutdown():
        log.info("остановка: завершаю сбор, ботов и доставку…")
        service.stop()
        if tunnel:
            tunnel.stop()
        dp = getattr(ctx, "dp", None)
        if dp is not None:               # Telegram: штатно остановить поллинг, без «Failed to fetch updates» в логе
            try:
                await asyncio.wait_for(dp.stop_polling(), 5)
            except Exception:  # noqa: BLE001 — поллинг не запущен (нет связи) или не успел — дальше отмена задач
                pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.max_bot.menu.close()
        if ctx.alerts.max._api:
            await ctx.alerts.max.api.close()
        if ctx.alerts._http:
            await ctx.alerts._http.aclose()
        ctx.db.event("info", "app", "Радар остановлен")

    app.on_startup(startup)
    app.on_shutdown(shutdown)
    secret_file = DATA_DIR / ".storage_secret"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_hex(32))
    from . import power

    try:
        ui.run(host=os.environ.get("HOST", "0.0.0.0"), port=args.port or ctx.settings.gui_port, title="Радар · Балтийский Дом",
               storage_secret=secret_file.read_text().strip(), reload=False, show=False, language="ru",
               show_welcome_message=False, favicon="📡")
    except KeyboardInterrupt:
        # Ctrl+C: веб-сервер уже штатно остановил сбор и ботов (on_shutdown), uvicorn лишь повторяет сигнал —
        # трассировка тут не нужна. Код 0 — start_radar.bat не перезапускает.
        log.info("Радар остановлен (Ctrl+C)")
    code = ctx.exit_code if ctx.exit_code is not None else power.EXIT_STOP
    log.info("Радар %s", "перезапускается" if code == power.EXIT_RESTART else "остановлен")
    raise SystemExit(code)


if __name__ in {"__main__", "__mp_main__"}:
    main()

