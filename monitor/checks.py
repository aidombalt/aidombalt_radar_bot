"""Проверки «из реального мира» одной командой (запускать на сервере/ноутбуке):
    start_radar.bat --netcheck     — что доступно из этой сети: ВК, Telegram, Google, госсайты, почта, туннели, Ollama
    start_radar.bat --brain-test   — 10 эталонных публикаций через локальную модель (скорость и качество)
Секреты в вывод не попадают никогда: ни целиком, ни в тексте ошибок."""
from __future__ import annotations

import asyncio
import importlib.util
import smtplib
import ssl
import time

import httpx

from .collectors import portable
from .settings import ROOT
from .util import make_http, relay_url


def _mask(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) > 5:
            text = text.replace(s, "***")
    return text


async def netcheck(ctx) -> None:
    s = ctx.settings
    relay = relay_url()
    secrets_ = [s.vk_service_token, s.vk_user_token, s.tg_bot_token, s.smtp.get("password", ""), s.bridge_token,
                relay, relay.rsplit("/", 1)[-1] if relay else ""]

    async def check(name: str, coro, hint: str = "") -> tuple[str, str, str]:
        t0 = time.monotonic()
        try:
            detail = await asyncio.wait_for(coro, 25)
            return "✅", name, f"{detail} · {time.monotonic() - t0:.1f} с"
        except Exception as e:
            msg = _mask(f"{type(e).__name__}: {e}", secrets_)[:160]
            return "❌", name, msg + (f"\n      → {hint}" if hint else "")

    async def http_ok(url: str, proxy: str | None = None, marker: str | None = None) -> str:
        async with make_http(proxy=proxy, timeout=15) as c:   # тот же клиент, что у сборщиков (с релеем)
            r = await c.get(url)
        if marker and marker not in r.text:
            raise RuntimeError(f"HTTP {r.status_code}, но страница не та (заглушка провайдера?)")
        return f"HTTP {r.status_code}"

    async def vk(base: str) -> str:
        tok = s.vk_service_token
        if not tok:
            raise RuntimeError("VK_SERVICE_TOKEN пуст")
        async with httpx.AsyncClient(timeout=15) as c:
            async def call(method, **params):
                r = await c.post(f"{base}/{method}", data={**params, "access_token": tok, "v": s.vk.api_version})
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"VK {j['error'].get('error_code')}: {j['error'].get('error_msg')}")
                return j["response"]
            who = await call("utils.resolveScreenName", screen_name="drozdenko_a")
            wall = await call("wall.get", domain="drozdenko_a", count=2)
        last = max((p.get("date", 0) for p in wall.get("items", [])), default=0)
        age = f"{(time.time() - last) / 3600:.0f} ч назад" if last else "—"
        return f"ключ рабочий: стена Дрозденко читается ({who.get('type')} {who.get('object_id')}), свежий пост {age}"

    async def tg_bot(proxy: str | None) -> str:
        tok = s.tg_bot_token
        if not tok:
            raise RuntimeError("TG_BOT_TOKEN пуст")
        async with make_http(proxy=proxy, timeout=15) as c:
            j = (await c.get(f"https://api.telegram.org/bot{tok}/getMe")).json()
        if not j.get("ok"):
            raise RuntimeError(j.get("description", "ошибка"))
        return f"бот @{j['result']['username']} на связи"

    def smtp_login() -> str:
        cfg = s.smtp
        if not cfg.get("host"):
            raise RuntimeError("SMTP_HOST пуст")
        tls = ssl.create_default_context()
        if cfg.get("starttls") or cfg["port"] == 587:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as m:
                m.starttls(context=tls)
                m.login(cfg["user"], cfg["password"])
        else:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=tls, timeout=20) as m:
                m.login(cfg["user"], cfg["password"])
        return f"вход в {cfg['host']} выполнен (письмо не отправлялось)"

    async def ollama() -> str:
        from .brain.core import LLM
        lc = s.brain.local
        llm = LLM(lc.base_url, lc.model, kind=lc.kind)
        try:
            ok, note = await llm.check()
        finally:
            await llm.close()
        if not ok:
            raise RuntimeError(note)
        return "модель на месте"

    async def tg_big() -> str:   # крупный канал: именно большие страницы «замирают» по дороге через Cloudflare
        async with make_http(timeout=45) as c:
            r = await c.get("https://t.me/s/a_beglov", headers={"X-Radar-After": "0", "X-Radar-Limit": "10"})
        kb, slim = r.num_bytes_downloaded / 1024, r.headers.get("x-radar-slim")
        if not slim:
            raise RuntimeError(f"страница {kb:.0f} КБ пришла без ужатия — в Cloudflare старая версия воркера")
        return f"{kb:.1f} КБ по сети, ужато воркером ({slim})"

    async def gnews() -> str:
        async with make_http(timeout=45) as c:
            r = await c.get(portable.gnews_url("Ленинградская область ЖКХ", "7d"))
        kb, slim = r.num_bytes_downloaded / 1024, r.headers.get("x-radar-slim", "")
        if "<rss" not in r.text:
            raise RuntimeError(f"HTTP {r.status_code}, но это не лента")
        if "rss" not in slim:
            raise RuntimeError(f"лента {kb:.0f} КБ без ужатия — в Cloudflare версия воркера старше v3")
        return f"{kb:.1f} КБ по сети, ужато ({slim})"

    async def port7844() -> str:   # туннель Cloudflare (мост для Colab) ходит на этот порт, а не на 443
        async def one(host):
            t0 = time.monotonic()
            r, w = await asyncio.wait_for(asyncio.open_connection(host, 7844), 8)
            w.close()
            return f"{host.split('.')[0]} {1000 * (time.monotonic() - t0):.0f} мс"
        return "TCP открыт: " + ", ".join(await asyncio.gather(one("region1.v2.argotunnel.com"),
                                                                one("region2.v2.argotunnel.com")))

    async def vps() -> str:   # TUNNEL=ssh: доступен ли VPS по SSH и отвечает ли мост снаружи
        import os

        from .tunnel import public_site
        host = os.environ.get("TUNNEL_HOST", "").strip()
        r, w = await asyncio.wait_for(asyncio.open_connection(host, 22), 10)
        w.close()
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                h = await c.get(f"https://{public_site(host)}/health")
                bridge = "мост отвечает ✅" if h.status_code == 200 else f"мост: HTTP {h.status_code} (Радар запущен?)"
            except Exception as e:  # noqa: BLE001
                bridge = f"мост пока не отвечает ({type(e).__name__}) — скрипт на VPS выполнен?"
        return f"SSH до {host} открыт; {bridge}"

    tp, fp = s.network.telegram_proxy, s.network.http_proxy_foreign
    via = " (через релей)" if relay else ""
    print("Проверяю сеть… (≈30 с)" + (f"\nРелей Cloudflare: {relay.rsplit('/', 1)[0]}/***" if relay else "") + "\n")
    rows = await asyncio.gather(
        check("ВК API (api.vk.com)", vk("https://api.vk.com/method"),
              "ошибка 1051 = ключ от приложения VK ID; нужен сервисный ключ мини-приложения (README → «Ключ ВК»)"),
        check("ВК API (api.vk.ru)", vk("https://api.vk.ru/method")),
        check("Telegram Bot API" + (via or " напрямую"), tg_bot(None),
              "бот не работает; алерты уходят почтой и через Colab. Решение: релей (README → «Релей»)"),
        *([check("Telegram Bot API через прокси", tg_bot(tp))] if tp else []),
        check("Telegram-каналы (t.me/s)" + via, http_ok("https://t.me/s/telegram", fp, "tgme_widget_message"),
              "без Colab каналы не опрашиваются — решение: релей"),
        *([check("Крупный TG-канал через релей", tg_big(),
                 "обновите код воркера из deploy/cloudflare_worker.js (v2), сохранив свою строку KEY")] if relay else []),
        check("Google News" + via, gnews() if relay else http_ok(portable.gnews_url("Мурино"), fp, "<rss"),
              "обновите код воркера (v3)" if relay else "без Colab не опрашивается — решение: релей"),
        check("Госсайт СПб (gov.spb.ru)", http_ok("https://www.gov.spb.ru/")),
        check("Госсайт ЛО (lenobl.ru)", http_ok("https://lenobl.ru/")),
        check("Cloudflare (для туннеля)", http_ok("https://www.cloudflare.com/cdn-cgi/trace", None, "ip="),
              "туннель cloudflared отсюда, скорее всего, не заработает"),
        *([check("Мост через VPS (TUNNEL=ssh)", vps(), "проверьте TUNNEL_HOST и что VPS включён")]
          if __import__("os").environ.get("TUNNEL", "").lower() == "ssh" else []),
        check("Туннель: порт 7844", port7844(),
              "мост для Colab не поднимется: попросите ИТ открыть исходящий TCP 7844 на *.argotunnel.com или берём VPS"),
        check("Почта (SMTP)", asyncio.to_thread(smtp_login),
              "Яндекс: Почта → Настройки → «Почтовые программы» → разрешить доступ; пароль приложения — типа «Почта»"),
        check("Локальная модель (Ollama)", ollama(), "запустите Ollama и скачайте модель (README)"),
    )
    w = max(len(r[1]) for r in rows)
    for icon, name, detail in rows:
        print(f"{icon} {name:<{w}}  {detail}")
    print("\nСкопируйте вывод целиком и отправьте в чат (секретов в нём нет).")


async def brain_test(ctx) -> None:
    """Тот же самотест, что в Colab, но против локальной модели (Ollama на ноутбуке/сервере)."""
    lc = ctx.settings.brain.local
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            probe = "/api/tags" if lc.kind == "ollama" else "/v1/models"
            (await c.get(f"{lc.base_url.rstrip('/')}{probe}")).raise_for_status()
    except Exception as e:
        print(f"❌ Локальная модель не отвечает ({lc.base_url}): {e}\n   Запущена ли Ollama? Значок Ollama в трее / `ollama serve`.")
        return
    spec = importlib.util.spec_from_file_location("brain_worker", ROOT / "colab" / "brain_worker.py")
    bw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bw)
    print(f"Модель: {lc.model} · {lc.base_url} · таймаут в бою {lc.timeout:.0f} с")
    await bw.selftest(str(ROOT), force_preset=lc.model, llm_url=lc.base_url, llm_kind=lc.kind)
    if lc.kind == "ollama":   # где на самом деле живёт модель: видеокарта или процессор
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                for m in (await c.get(f"{lc.base_url.rstrip('/')}/api/ps")).json().get("models", []):
                    size, vram = m.get("size", 0), m.get("size_vram", 0)
                    where = "целиком в видеопамяти" if size and vram >= size * 0.98 else \
                        f"⚠️ в видеопамяти только {vram / 2**30:.1f} из {size / 2**30:.1f} ГБ — остальное на процессоре"
                    print(f"Ollama: {m.get('name')} — {where}")
        except Exception:
            pass
    print(f"Ориентир: до {lc.timeout / 3:.0f} с на публикацию — норма; ближе к {lc.timeout:.0f} с — модель не успевает, "
          "в бою разбор уйдёт на правила.")
