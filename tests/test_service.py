"""Планировщик: кто опрашивает Telegram, когда Colab выключен (а он выключен большую часть суток)."""
import asyncio

from monitor.brain.core import extract_json, strip_thinking
from monitor.service import Service
from monitor.util import iso

ALL = ("hot", "warm", "cold")


class Resp:
    status_code, text = 200, '<div class="tgme_widget_message">пост</div>'


class Client:
    async def get(self, *a, **k):
        return Resp()


def test_telegram_fallback_follows_colab(ctx):
    svc = Service(ctx)
    svc.collectors["tg_channel"].client = Client()
    run = lambda: asyncio.run(svc._foreign_fallback(ALL))  # noqa: E731

    got = run()                                   # Colab ни разу не приходил, t.me открывается
    assert got and {s["kind"] for s in got} == {"tg_channel"} and ctx.brain.foreign_mode == "server"

    ctx.db.x("INSERT INTO brain_workers(worker_id, last_seen, info_json) VALUES('colab-x', ?, '{}')", (iso(),))
    assert run() == [] and ctx.brain.foreign_mode == "colab"      # Colab на связи — сервер не дублирует

    ctx.db.x("DELETE FROM brain_workers")
    Resp.status_code, svc._tg_probe_at = 403, -1e9                # t.me из сети офиса закрыт
    try:
        assert run() == [] and ctx.brain.foreign_mode == "none"
    finally:
        Resp.status_code = 200


def test_gemma_thinking_is_stripped():
    raw = '<|channel>thought\nHere {is} a plan<channel|>{"priority": "P2"}'
    assert extract_json(raw) == {"priority": "P2"}
    assert strip_thinking("<think>план</think>Текст записки") == "Текст записки"


def test_tunnel_guard():
    from monitor.netguard import LocalOnly

    g = LocalOnly(None)
    sc = lambda path, ip, *hdr: {"type": "http", "path": path, "client": (ip, 5000), "headers": list(hdr)}  # noqa: E731
    assert g.allowed(sc("/", "192.168.1.20"))                                          # офис напрямую
    assert g.allowed(sc("/", "127.0.0.1", (b"x-forwarded-for", b"192.168.1.20")))      # офис через свой Caddy
    assert not g.allowed(sc("/", "127.0.0.1", (b"cf-connecting-ip", b"34.1.2.3")))     # интернет через cloudflared
    assert not g.allowed(sc("/", "127.0.0.1", (b"x-forwarded-for", b"10.0.0.1, 34.1.2.3")))  # подделка не спасает
    assert not g.allowed(sc("/", "34.1.2.3"))                                           # проброс порта наружу
    assert g.allowed(sc("/api/brain/lease", "127.0.0.1", (b"cf-connecting-ip", b"34.1.2.3")))  # мост открыт
    assert g.allowed(sc("/health", "34.1.2.3"))


def test_relay_rewrites_only_blocked_hosts():
    import httpx

    from monitor.util import RelayTransport

    seen = []
    inner = httpx.MockTransport(lambda r: (seen.append((str(r.url), r.headers["host"])), httpx.Response(200))[1])

    async def go():
        async with httpx.AsyncClient(transport=RelayTransport("https://radar-relay.x.workers.dev/K3Y", inner)) as c:
            await c.get("https://t.me/s/murino_life?before=5")
            await c.post("https://api.telegram.org/bot1:AA/sendMessage", json={"a": 1})
            await c.get("https://api.vk.com/method/wall.get")          # ВК — напрямую
    asyncio.run(go())
    assert seen == [("https://radar-relay.x.workers.dev/K3Y/t.me/s/murino_life?before=5", "radar-relay.x.workers.dev"),
                    ("https://radar-relay.x.workers.dev/K3Y/api.telegram.org/bot1:AA/sendMessage", "radar-relay.x.workers.dev"),
                    ("https://api.vk.com/method/wall.get", "api.vk.com")]


def test_unstarted_bot_chat_is_not_network_failure(ctx):
    """«chat not found» = человек не нажал «Старт»: не отдаём ретранслятору, ждём и шлём сразу после /start."""
    al = ctx.alerts
    calls = []

    async def fail(payload):
        calls.append(payload["chat_id"])
        raise RuntimeError("Telegram 400: Bad Request: chat not found")

    al.send_tg = fail
    al.enqueue("telegram", "100200300", {"chat_id": "100200300", "text": "тест"}, "t1")
    for _ in range(4):
        ctx.db.x("UPDATE outbox SET next_attempt_at=?", (iso(),))
        asyncio.run(al.process())
    row = ctx.db.one("SELECT * FROM outbox WHERE dedup_key='t1'")
    assert row["status"] == "pending" and row["attempts"] == 4           # не «relay», не «dead»
    assert ctx.db.one("SELECT 1 FROM events WHERE message LIKE '%нажать «Старт»%'")

    async def ok(payload):
        calls.append("ok")
    al.send_tg = ok
    ctx.db.x("UPDATE outbox SET next_attempt_at='2999-01-01' WHERE dedup_key='t1'")
    assert al.recipient_ready("100200300") == 1                         # /start → отправка без ожидания
    asyncio.run(al.process())
    assert ctx.db.one("SELECT status FROM outbox WHERE dedup_key='t1'")["status"] == "sent"


def test_media_without_rss_goes_via_google_news(ctx, monkeypatch):
    from monitor.collectors import portable
    from monitor.collectors.web import RSSCollector

    async def no_feed(client, url):
        return None

    got = {}

    class C:
        async def get(self, url, headers=None):
            got["url"] = url
            return type("R", (), {"status_code": 200, "headers": {}, "content": b"<rss><channel></channel></rss>"})()

    monkeypatch.setattr(portable, "discover_rss", no_feed)
    col = RSSCollector(ctx)
    col.client = C()
    ctx.db.upsert_source({"key": "rss:https://47channel.ru/", "kind": "rss", "handle": "https://47channel.ru/",
                          "title": "47 канал", "tier": "warm", "category": "media", "where": "server"})
    src = {"key": "rss:https://47channel.ru/", "handle": "https://47channel.ru/", "meta": {}}
    assert asyncio.run(col.fetch(src)) == []
    assert "news.google.com" in got["url"] and "site%3A47channel.ru" in got["url"]
    from monitor.util import jload
    meta = jload(ctx.db.one("SELECT meta_json FROM sources WHERE key='rss:https://47channel.ru/'")["meta_json"], {})
    assert meta["via_gnews"] and "news.google.com" in meta["feed"]          # запомнили — автопоиск больше не гоняем


def test_evidence_caps_real_noise_from_pilot(ctx):
    """Пилот 24.09: жалобы без наших адресов с губернаторских страниц будили людей как P2. Теперь — фон."""
    cap = lambda text, rel="cluster", cat="governor", **it: ctx.pipeline.evidence_cap(  # noqa: E731
        {"platform": "vk_comment", **it}, ctx.matcher.match(text, source_category=cat).to_json(), {"relation": rel})[0]
    # реальные комментарии с пилота: Отрадное/лифт у Дрозденко, отопление у Беглова
    assert cap("[club192680652|Комитет госжилнадзора Ленинградской области], какой осмотр? Управляющая компания "
               "и аварийная в курсе! Лифт стоит с 13 сентября!") == "P4"
    assert cap("Почему в этом году творится какое-то мракобесие с отоплением и горячей водой? Профилактическое "
               "отключение горячей воды начали 19 мая. Комитет по энергетике твердит про +8", rel="our") == "P3"
    # а вот это по-прежнему может будить
    assert cap("ЖК Виктория, Авиаторов Балтики 9к1 — лифт стоит вторую неделю!") == "P1"
    assert cap("У нас тоже лифт не работает", parent_text="Жители ЖК Лампо жалуются губернатору") == "P1"
    assert cap("В Мурино управляйка опять не вывезла мусор, пишем губернатору") == "P3"


def test_small_model_cannot_page_on_cluster():
    from monitor.brain.core import normalize_analysis

    a = normalize_analysis({"relevant": True, "relation": "cluster", "priority": "P2", "summary": "x", "why": "y",
                            "action": "P2", "sentiment": -1, "tone": "жалоба", "topics": []})
    assert a["priority"] == "P3" and a["action"] and a["action"] != "P2"


PILOT_2 = [  # вторая пачка с пилота 24.09 — пришли уже после /start из старой очереди
    "Здравствуйте, Александр Юрьевич! Только все не так хорошо во дворе дома в Мурино по адресу улица Шувалова,дом 16/9. "
    "Вместо подземного паркинга склады, разгрузка, доставщики на самокатах по газонам",
    "Мурино Воронцовскй 19 к 1. Все тоже самое. Также ТУК СПб , таже разруха. Абсолютное бездействие КГЖН. [фото]",
    "[id49538|Дмитрий], а после потопа остается мусор и никто его сутки не убирает! Скоро наш дом рухнет [фото]",
    "[club178555683|Александр Юрьевич]! Мой дом не спрашивая собственников отдали под управление бездельников. "
    "Вынесли общее имущество дома, переломали электрику, дом утопает в мусоре. Думаете мы это все стерпим?",
    "[club192680652|Комитет госжилнадзора Ленинградской области], Мурино, Шувалова 16/9. Никого не предупредили, что "
    "сигнальное оборудование противопожарной системы будет отключено. [club51009754|МЧС Ленинградской области]",
]


def test_pilot_batch_2_does_not_page(ctx):
    for text in PILOT_2:
        m = ctx.matcher.match(text, source_category="governor").to_json()
        cap, why = ctx.pipeline.evidence_cap({"platform": "vk_comment"}, m, {"relation": "cluster"})
        assert cap in ("P3", "P4"), (text[:40], cap, why)


def _alert_item(ctx, prio="P2"):
    iid = ctx.db.x("INSERT INTO items(platform, external_id, source_key, text, author_name, priority, workflow, "
                   "analysis_json, tags, published_at, collected_at) VALUES('vk_comment','vk:1_2_3','vk_wall:drozdenko_a',"
                   "'ЖК Лампо — двор в грязи','Житель',?, 'open','{\"summary\": \"Жалоба на двор\"}','[\"ЖК:Лампо\"]',?,?)",
                   (prio, iso(), iso()))
    return iid


def test_cards_change_in_place_for_everyone(ctx, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "1:TEST")
    al, sent = ctx.alerts, []

    async def tg(method, payload):
        sent.append((method, payload.get("chat_id"), payload.get("message_id"), payload.get("text", "")[:30],
                     payload.get("reply_markup")))
        return {"message_id": 700 + len(sent)}
    al._tg = tg
    iid = _alert_item(ctx)
    ctx.db.x("INSERT INTO users(tg_id, name, role, level, active) VALUES('222','Коллега','manager',1,1)")
    al.raise_alert(iid, "P2")
    asyncio.run(al.process())
    copies = ctx.db.q("SELECT chat_id, message_id FROM alert_msgs WHERE item_id=?", (iid,))
    assert len(copies) >= 2                                   # запомнили, куда ушли карточки

    sent.clear()
    al.ack(iid, "Оператор", "false_positive")
    assert asyncio.run(al.refresh_cards(iid)) == len(copies)
    assert all(s[0] == "editMessageText" and s[3].startswith("❌ <b>Ложное") for s in sent)   # свернуто у всех
    assert sent[0][4]["inline_keyboard"][0][0]["callback_data"] == f"a:undo:{iid}"

    sent.clear()
    al.reopen(iid, "Оператор")                                # «↩️ Вернуть»
    it = ctx.db.item(iid)
    assert it["workflow"] == "open" and it["priority"] == "P2"
    assert not ctx.db.one("SELECT 1 FROM feedback WHERE item_id=? AND verdict='false_positive'", (iid,))
    asyncio.run(al.refresh_cards(iid))
    assert "Беру в работу" in str(sent[0][4])                 # полная карточка с кнопками вернулась


def test_stale_queued_alert_is_not_sent(ctx):
    al, sent = ctx.alerts, []

    async def tg(method, payload):
        sent.append(method)
        return {"message_id": 1}
    al._tg = tg
    iid = _alert_item(ctx)
    al.raise_alert(iid, "P2")
    ctx.db.x("UPDATE items SET priority='P4' WHERE id=?", (iid,))       # пока лежало в очереди — понизили
    asyncio.run(al.process())
    assert not sent and ctx.db.one("SELECT COUNT(*) n FROM outbox WHERE status='dropped'")["n"] >= 1


def test_builtin_tunnel_reports_url(ctx, tmp_path, monkeypatch):
    """Радар сам держит туннель и присылает адрес моста в бот; при смене адреса — присылает новый."""
    import os
    import sys

    from monitor import tunnel as tun

    fake = tmp_path / "cloudflared"
    fake.write_text(f"#!{sys.executable}\nimport time\n"
                    "print('INF Requesting new quick Tunnel on trycloudflare.com...', flush=True)\n"
                    "print('INF |  https://calm-river-demo.trycloudflare.com  |', flush=True)\n"
                    "print('INF Registered tunnel connection connIndex=0 location=ams01 protocol=http2', flush=True)\n"
                    "time.sleep(30)\n")
    os.chmod(fake, 0o755)
    monkeypatch.setattr(tun, "find_cloudflared", lambda: str(fake))
    monkeypatch.setattr(tun, "PROBE_FIRST", 0)
    t = tun.Tunnel(ctx, 8080)

    async def ok_probe(url):
        return True, "✅ тест"
    t.probe = ok_probe                                   # «сходили снаружи на /health — ответ есть»

    async def go():
        task = asyncio.create_task(t.run())
        for _ in range(50):
            await asyncio.sleep(0.1)
            if ctx.brain.tunnel_url:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert ctx.brain.tunnel_url == "https://calm-river-demo.trycloudflare.com"
    note = ctx.db.one("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'adm:tunnel:%'")
    assert note and "RADAR_URL" in note["payload_json"]


def test_tunnel_without_edge_connection_is_not_announced(ctx, tmp_path, monkeypatch):
    """Пилот: адрес пришёл, а Colab получил 530 — туннель так и не соединился (порт 7844 закрыт)."""
    import os
    import sys

    from monitor import tunnel as tun

    fake = tmp_path / "cloudflared"
    fake.write_text(f"#!{sys.executable}\nimport time\n"
                    "print('INF |  https://dead-end-demo.trycloudflare.com  |', flush=True)\n"
                    "print('ERR Failed to dial a quic connection error=timeout', flush=True)\ntime.sleep(30)\n")
    os.chmod(fake, 0o755)
    monkeypatch.setattr(tun, "find_cloudflared", lambda: str(fake))
    monkeypatch.setattr(tun, "CONNECT_TIMEOUT", 1)
    t = tun.Tunnel(ctx, 8080)

    async def go():
        t.proc = await asyncio.create_subprocess_exec(str(fake), stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.STDOUT)
        ok = await t._watch("quic", lambda url: None)
        t.stop()
        await t.proc.wait()
        return ok
    assert asyncio.run(go()) is False
    assert ctx.brain.tunnel_url is None                                   # адрес-пустышку не рассылаем
    assert ctx.db.one("SELECT 1 FROM events WHERE component='tunnel' AND message LIKE '%7844%'")


def test_tunnel_registered_but_dead_is_restarted_not_announced(ctx, tmp_path, monkeypatch):
    """Пилот 24.09, вторая попытка: «Registered tunnel connection» есть, а снаружи 530. Адрес не рассылаем,
    туннель перезапускаем с другим протоколом."""
    import os
    import sys

    from monitor import tunnel as tun

    fake = tmp_path / "cloudflared"
    fake.write_text(f"#!{sys.executable}\nimport time\n"
                    "print('INF |  https://zombie-demo.trycloudflare.com  |', flush=True)\n"
                    "print('INF Registered tunnel connection connIndex=0 protocol=http2', flush=True)\ntime.sleep(30)\n")
    os.chmod(fake, 0o755)
    for k, v in (("PROBE_FIRST", 0), ("PROBE_RETRY", 0), ("PROBE_FAILS_TO_RESTART", 2)):
        monkeypatch.setattr(tun, k, v)
    monkeypatch.setattr(tun, "find_cloudflared", lambda: str(fake))
    t = tun.Tunnel(ctx, 8080)
    probes = []

    async def dead(url):
        probes.append(url)
        return False, "❌ HTTP 530 error code: 1033"
    t.probe = dead

    async def go():
        task = asyncio.create_task(t.run())
        for _ in range(60):
            await asyncio.sleep(0.1)
            if t.fails:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert len(probes) >= 2 and t.fails >= 1
    assert ctx.brain.tunnel_url is None
    assert not ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key LIKE 'adm:tunnel:https%'")
    assert ctx.db.one("SELECT 1 FROM events WHERE component='tunnel' AND message LIKE '%1033%'")


def test_ssh_bridge_prepares_vps_script_and_asks_for_setup(ctx, tmp_path, monkeypatch):
    """Мост через свой VPS: ключ и готовый скрипт для VPS создаются сами; «Permission denied» → понятная подсказка."""
    import os
    import sys

    from monitor import tunnel as tun

    monkeypatch.setenv("TUNNEL", "ssh")
    monkeypatch.setenv("TUNNEL_HOST", "185.10.20.30")
    monkeypatch.setattr(tun, "DATA_DIR", tmp_path)
    fake_ssh, fake_kg = tmp_path / "ssh", tmp_path / "ssh-keygen"
    fake_ssh.write_text(f"#!{sys.executable}\nimport sys\n"
                        "print('radar@185.10.20.30: Permission denied (publickey).', flush=True)\nsys.exit(255)\n")
    fake_kg.write_text(f"#!{sys.executable}\nimport sys\nf = sys.argv[sys.argv.index('-f') + 1]\n"
                       "open(f, 'w').write('PRIVATE')\nopen(f + '.pub', 'w').write('ssh-ed25519 AAAAC3FAKEKEY radar-bridge')\n")
    for f in (fake_ssh, fake_kg):
        os.chmod(f, 0o755)
    monkeypatch.setattr(tun, "_find", lambda name, extra: str(fake_ssh if name == "ssh" else fake_kg))
    t = tun.make_tunnel(ctx, 8080)
    assert isinstance(t, tun.SshTunnel) and t.site == "185-10-20-30.sslip.io"

    async def go():
        task = asyncio.create_task(t.run())
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    ready = (tmp_path / "vps_setup_ready.sh").read_text()
    assert "ssh-ed25519 AAAAC3FAKEKEY" in ready and "SITE='185-10-20-30.sslip.io'" in ready and "PORT='18080'" in ready
    assert "__PUBKEY__" not in ready and "__SITE__" not in ready
    assert ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key LIKE 'adm:tunnel-sshkey:%'")
    assert tun.make_tunnel(ctx, 8080) is not None


def test_rematch_cleans_old_mistakes(ctx):
    """После обновления: пост ГЖИ от марта с «на 5 этаже» больше не «УК Этажи», а тёзка из Удомли — не сосед."""
    from monitor.util import jdump
    rows = [("tg", "tg_channel:gzhi_spb", "Госжилинспекция: детонация газа на 5 этаже дома на Товарищеском проспекте",
             "2026-03-19T12:43:00Z"),
            ("vk_post", "vk_search:КоммуналСервис", "В Удомле прокуратура выявила нарушения в деятельности УК "
             "ООО \"Коммуналсервис\" по проспекту Курчатова", iso())]
    ids = []
    for plat, key, text, pub in rows:
        ids.append(ctx.db.x("INSERT INTO items(platform, external_id, source_key, text, priority, relevant, analysis_json, "
                            "analysis_level, tags, published_at, collected_at, analyzed_at, workflow) "
                            "VALUES(?,?,?,?, 'P3', 1, ?, 'local', ?, ?, ?, ?, 'open')",
                            (plat, f"x:{key}", key, text, jdump({"priority": "P3", "relevant": True, "relation": "our"}),
                             jdump(["УК:«Этажи»"]), pub, iso(), iso())))
    assert ctx.pipeline.rematch_recent() == 2
    for iid in ids:
        it = ctx.db.item(iid)
        assert it["priority"] == "P4" and not it["relevant"], dict(it)
