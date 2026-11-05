"""Пилот 24–25.09: вечернее отключение света в Мурино. Ответы в каналах, правки постов, динамика, Google News 503,
утренний отчёт с открытым инцидентом, волна публикаций без повторных алертов."""
import asyncio
from datetime import timedelta

import httpx

from monitor.collectors import portable
from monitor.util import iso, jdump, now_utc

REPLY_HTML = """<html><div class="tgme_channel_info_header_title"><span>Девяткино онлайн | Мурино</span></div>
<div class="tgme_widget_message js-widget_message" data-post="news9online/10352">
<a class="tgme_widget_message_reply" href="https://t.me/news9online/10351"><div class="tgme_widget_message_text js-message_reply_text"
 dir="auto">Photo, Массовые отключения света в западном и…</div></a>
<div class="tgme_widget_message_text js-message_text" dir="auto">1. Только что звонил в УК, сказали, что у них авария</div>
<span class="tgme_widget_message_views">15.7K</span><time datetime="2026-09-24T16:33:42+00:00"></time></div></html>"""

EDITED_HTML = """<html><div class="tgme_widget_message js-widget_message" data-post="news9online/10351">
<div class="tgme_widget_message_text js-message_text" dir="auto">Массовые отключения света в западном Мурино:<br/>
1. В доме 27 по Шувалова свет пропал в 18:40.<br/>2. Куда пропал свет в «Цветах радуги»?<br/>3. Нет света на Воронцовском</div>
<span class="tgme_widget_message_views">17.8K</span><time datetime="2026-09-24T15:59:07+00:00"></time></div></html>"""


def test_reply_post_keeps_own_text():
    items, _ = portable.parse_tg_page(REPLY_HTML, "news9online")
    it = items[0]
    assert it["text"].startswith("1. Только что звонил в УК") and it["views"] == 15700
    assert it["parent_external_id"] == "tg:news9online/10351" and "Массовые отключения" in it["parent_text"]


def test_recheck_rereads_last_posts():
    seen = {}

    def handler(req: httpx.Request):
        seen["after"] = req.headers.get("x-radar-after")
        return httpx.Response(200, text=EDITED_HTML + REPLY_HTML)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items, _ = asyncio.run(portable.fetch_tg_channel(client, "news9online", since_id=10352, recheck=4))
    assert seen["after"] == "10348" and {i["msg_id"] for i in items} == {10351, 10352}


def test_list_number_is_not_a_house(ctx):
    m = ctx.matcher.match('2. Куда пропал свет в "Цветах радуги"? 3. Нет света по Воронцовскому', "", "public", "LO")
    assert any(h.kind == "zhk" and h.key == "rainbow" and h.conf >= 0.6 for h in m.hits)


def test_edited_post_and_dynamics(ctx):
    from monitor.service import Service

    svc = Service(ctx)
    svc.collectors["tg_channel"].client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=EDITED_HTML)))
    src = {"key": "tg_channel:news9online", "kind": "tg_channel", "handle": "news9online", "title": "Девяткино онлайн",
           "category": "public", "region": "LO", "tier": "hot"}
    ctx.db.upsert_source(src)
    ctx.pipeline.ingest([{"platform": "tg", "external_id": "tg:news9online/10351", "url": "https://t.me/news9online/10351",
                          "author_id": "tg:news9online", "text": "Массовое отключение света в западном Мурино: 1. В доме 27 "
                          "по Шувалова свет пропал в 18:40.", "published_at": iso(), "views": 848}], src)
    iid = ctx.db.one("SELECT id FROM items WHERE external_id='tg:news9online/10351'")["id"]
    assert asyncio.run(svc.refresh_item_metrics(iid))
    it = ctx.db.item(iid)
    assert "Цветах радуги" in it["text"] and it["edited"] == 1 and it["views"] == 17800
    assert "rainbow" in it["match_json"]                                   # правку сопоставили заново
    ctx.db.x("INSERT INTO metrics(item_id, ts, likes, comments, reposts, views) VALUES(?,?,0,0,0,997)",
             (iid, iso(now_utc() - timedelta(hours=12))))
    from aiogram.methods import EditMessageText, SendMessage
    from aiogram.types import CallbackQuery, Message, Update, User

    from monitor.alerts.bot import build_bot
    from tests.test_bot import CHAT, NOW, MockSession
    sess = MockSession()
    bot, dp = build_bot(ctx, svc, session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    asyncio.run(dp.feed_update(bot, Update(update_id=3, callback_query=CallbackQuery(
        id="d1", from_user=User(id=111, is_bot=False, first_name="Оператор"), chat_instance="x", data=f"a:dyn:{iid}",
        message=Message(message_id=9, date=NOW, chat=CHAT, text="card")))))
    reply = [m.text for m in sess.sent if isinstance(m, EditMessageText)][-1]
    assert not [m for m in sess.sent if isinstance(m, SendMessage)]
    assert "рост просмотров +16 803" in reply and "17 800" in reply


def test_gnews_pause(ctx, monkeypatch):
    from monitor.collectors.web import GNewsCollector, gnews_pause

    col = GNewsCollector(ctx)
    col.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503, text="unusual traffic")))
    srcs = [dict(r) for r in ctx.db.q("SELECT * FROM sources WHERE kind='gnews'")] or [
        {"key": "gnews:УК Этажи", "kind": "gnews", "handle": "УК Этажи"}]
    for s in srcs[:1]:
        ctx.db.upsert_source({**s, "where": "server"})
    st = asyncio.run(col.run(col.plan(srcs)[:1]))
    assert st["err"] == 0 and st.get("paused") == 1 and gnews_pause(ctx)
    assert col.plan(srcs) == []                                          # на паузе никто не стучится
    row = ctx.db.one("SELECT consecutive_failures FROM sources WHERE key=?", (srcs[0]["key"],))
    assert not row or not row["consecutive_failures"]                    # пауза — не поломка источника


def _evening_incident(ctx, hours_ago=14):
    """Как 24.09: пост про отключение в Мурино с «Цветами радуги», P2, взят в работу утром."""
    t0 = now_utc() - timedelta(hours=hours_ago)
    ctx.db.upsert_source({"key": "tg_channel:news9online", "kind": "tg_channel", "handle": "news9online",
                          "title": "Девяткино онлайн | Мурино", "category": "public", "region": "LO"})
    ctx.db.upsert_source({"key": "vk_wall:mo_murino", "kind": "vk_wall", "handle": "mo_murino",
                          "title": "Администрация Мурино — ВК", "category": "official", "region": "LO"})
    iid = ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at, analyzed_at, "
                   "priority, relevant, workflow, tags, analysis_json, views, author_followers, candidate) "
                   "VALUES('tg','tg:news9online/10352','tg_channel:news9online','https://t.me/news9online/10352',"
                   "'Куда пропал свет в «Цветах радуги»?',?,?,?,'P2',1,'in_work',?,?,997,59100,1)",
                   (iso(t0), iso(t0), iso(t0), jdump(["ЖК:Цвета Радуги", "кластер:Мурино", "тема:электричество"]),
                    jdump({"summary": "Массовые отключения света в Мурино, упомянут ЖК «Цвета Радуги»",
                           "action": "Ответить жителям от лица УК", "topics": ["электричество"]})))
    ctx.db.x("INSERT INTO alerts(item_id, priority, created_at, last_notified_at, acked_by, acked_at) VALUES(?,?,?,?,?,?)",
             (iid, "P2", iso(t0), iso(t0), "leva protiv", iso(now_utc() - timedelta(hours=1))))
    for k, (src, text) in enumerate([("vk_wall:mo_murino", "Аварийное отключение: авария на подстанции «Ручьи», 2 часа"),
                                     ("tg_channel:news9online", "Ещё дома без света в Мурино"),
                                     ("tg_channel:news9online", "Свет дали не везде")]):
        t = t0 + timedelta(minutes=10 * (k + 1))
        ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at, analyzed_at, "
                 "priority, relevant, workflow, tags, analysis_json) VALUES('vk_post',?,?,?,?,?,?,?,'P3',1,'open',?,?)",
                 (f"x{k}", src, f"https://example/{k}", text, iso(t), iso(t), iso(t),
                  jdump(["кластер:Мурино", "тема:электричество"]), jdump({"summary": text, "topics": ["электричество"]})))
    return iid


def test_morning_report_after_evening_incident(ctx):
    from monitor.reports import builder, situation

    iid = _evening_incident(ctx)
    d = builder.gather(ctx, iso(now_utc() - timedelta(hours=10)), iso())          # период начался ПОСЛЕ инцидента
    lines = situation.state_lines(d)
    assert d["head"]["level"] == "orange" and lines[0].startswith("🟠 Открыт инцидент P2 · ЖК Цвета Радуги (Мурино)")
    assert "в работе у leva protiv" in lines[0]
    assert "новых упоминаний наших объектов не было" in lines[1]
    st = d["stories"][0]
    assert st["matter"] == iid and st["n"] == 4 and st["facts"][0]["source"] == "Администрация Мурино — ВК"
    r = asyncio.run(builder.build(ctx, hours=10, send=False))
    assert r["level"] == "orange" and "открыт инцидент" in r["subject"]
    import docx
    text = "\n".join(p.text for p in docx.Document(r["docx"]).paragraphs)
    assert "Открыт инцидент P2" in text and "подстанции «Ручьи»" in text


def test_quiet_report_is_green(ctx):
    from monitor.reports import builder

    d = builder.gather(ctx, iso(now_utc() - timedelta(hours=3)), iso())
    assert d["head"]["level"] == "green" and not d["head"]["after"]


def test_wave_does_not_alert_twice(ctx):
    iid = _evening_incident(ctx, hours_ago=2)
    ctx.db.x("UPDATE items SET workflow='open' WHERE id=?", (iid,))
    ctx.pipeline.ingest([{"platform": "tg", "external_id": "tg:news9online/10351", "url": "https://t.me/news9online/10351",
                          "author_id": "tg:news9online", "text": "Мурино: нет света в ЖК «Цвета Радуги» на Шувалова 44 и "
                          "на Воронцовском, уже час", "published_at": iso()}],
                        {"key": "tg_channel:news9online", "category": "public", "region": "LO"})
    new = ctx.db.one("SELECT id FROM items WHERE external_id='tg:news9online/10351'")["id"]
    ctx.pipeline.finalize(new, {"relevant": True, "priority": "P2", "relation": "our", "sentiment": -1,
                                "summary": "нет света", "topics": ["электричество"]}, "local")
    assert ctx.db.item(new)["priority"] == "P2"
    assert not ctx.db.one("SELECT 1 FROM alerts WHERE item_id=?", (new,))           # второй алерт не будит людей
    fu = ctx.alerts.followups(ctx.db.item(iid))
    assert fu and fu["n"] >= 4


# ---------------------------------------------------------------- выходные 26–27.09
def test_night_policy_and_crime_cap(ctx, monkeypatch):
    """Кудрово: «мужчина с разбитой головой у лифта» — полиция/охрана, P3 без алерта.
    Прочие P2 ночью ждут утра, ГЖИ/СМИ/массовые аварии — будят сразу."""
    from monitor import workhours
    from monitor.util import jdump

    ctx.db.upsert_source({"key": "tg_channel:dtp_chp_kudrovo1", "kind": "tg_channel", "handle": "dtp_chp_kudrovo1",
                          "title": "ДТП и ЧП Кудрово", "category": "public", "region": "LO"})
    body = ("Европейский, 13к2, подъезд 2. У лифта находится мужчина в синей жилетке. У него разбита голова. "
            "Он заходит в лифт только вместе с мужчинами. Будьте внимательны.")
    m = ctx.matcher.match(body, "", "public", "LO").to_json()
    cap, why = ctx.pipeline.evidence_cap({"platform": "tg", "source_key": "tg_channel:dtp_chp_kudrovo1", "text": body}, m, {})
    assert cap == "P3" and "полиция" in why

    monkeypatch.setattr(workhours, "is_off", lambda c, ts=None: True)                  # суббота, вечер
    def item(text, src="tg_channel:dtp_chp_kudrovo1", topics=("лифт",)):
        iid = ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at, priority,"
                       " tags, analysis_json, match_json, workflow) VALUES('tg',?,?,?,?,?,?,'P2',?,?,'{}','open')",
                       (f"tg:x/{abs(hash(text)) % 10**8}", src, "https://t.me/x/1", text, iso(), iso(),
                        jdump([f"тема:{t}" for t in topics]), jdump({"topics": list(topics)})))
        ctx.alerts.raise_alert(iid, "P2")
        return ctx.db.one("SELECT next_attempt_at, payload_json FROM outbox WHERE item_id=? AND channel='telegram'", (iid,))
    held = item("ЖК Прогресс, Европейский 13: лифт не работает второй день")
    assert held["next_attempt_at"] > iso() and "отложено до" in held["payload_json"]
    now = item("Жители ЖК Прогресс пожаловались в ГЖИ на управляющую компанию", topics=("суд_прокуратура_надзор",))
    assert now["next_attempt_at"] <= iso() and "отложено" not in now["payload_json"]
    mass = item("Массовые отключения света в Мурино, ЖК Цвета Радуги без света", topics=("электричество",))
    assert mass["next_attempt_at"] <= iso()


def test_subscriptions_routing(ctx):
    from monitor.alerts import subscriptions as subs

    ctx.db.x("INSERT INTO users(tg_id, name, role, level, active) VALUES('777','Наблюдатель','viewer',1,1)")
    ctx.set_override("report_emails", ["director@dombalt.ru"])
    rows = {r["id"]: r for r in subs.rows(ctx)}
    assert "service" in rows["tg:111"]["events"] and "service" not in rows["tg:777"]["events"]
    assert set(rows["mail:director@dombalt.ru"]["events"]) == {"p1", "esc", "rep_morning", "rep_weekly"}
    assert ctx.alerts.recipients("p2", "mail") == []                                   # P2 руководству на почту — нет
    subs.save(ctx, "mail:director@dombalt.ru", ["p1"])
    assert ctx.alerts.recipients("rep_morning", "mail") == []
    ctx.alerts.admin_notice("🔧 тест", "t1")
    assert not ctx.db.one("SELECT 1 FROM outbox WHERE channel='email' AND dedup_key LIKE 'admmail:t1%'")
    assert ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key='adm:t1:111'")
    # в настройках один человек — одна строка: почта сотрудника приклеивается к его Telegram
    ctx.db.x("INSERT INTO users(tg_id, name, role, level, active, email) VALUES('888','Никита','manager',2,1,'nik@dombalt.ru')")
    ppl = {p["name"]: p for p in subs.people(ctx)}
    assert ppl["Никита"]["tg"]["id"] == "tg:888" and ppl["Никита"]["mail"]["id"] == "mail:nik@dombalt.ru"
    assert ppl["director@dombalt.ru"]["tg"] is None and ppl["director@dombalt.ru"]["mail"]
    assert len([p for p in subs.people(ctx) if p["mail"] and p["mail"]["address"] == "nik@dombalt.ru"]) == 1


def test_google_news_links(ctx, monkeypatch):
    import httpx

    from monitor import links

    gid = "CBMipwFBVV95cUxNSjMwRTFrQnJST1paenRJNlNTQW5VYldDVlg0OVgzQ2dlc045OG5nU1FvOFBnSmNLQUlTWE41Um9vWE5QMmZ4ZDFD"
    url = f"https://news.google.com/rss/articles/{gid}?oc=5"
    assert links.gn_id(url) == gid and links.decode_offline(gid) is None               # новый формат — нужен релей
    old = "CBMiQmh0dHBzOi8vd3d3LmZvbnRhbmthLnJ1LzIwMjYvMDkvMjcvNzY2MDAwMDAvP3V0bT1nb29nbGUtbmV3c9IBAA"
    assert links.decode_offline(old).startswith("https://www.fontanka.ru/2026/09/27/")
    monkeypatch.setenv("RELAY_URL", "https://relay.example/KEY")
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        return httpx.Response(200, json={"url": "https://www.spbdnevnik.ru/news/2026-09-27/gzhi-114", "how": "batchexecute"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items = [{"platform": "gnews", "external_id": f"gnews:{gid}", "url": url, "source_url": "https://www.spbdnevnik.ru",
              "title": "ГЖИ выявила нарушения в ЖК «Монплезир» на Ленинском, 114 - Петербургский дневник",
              "text": "УК Петербургский дом получила предписание ГЖИ"},
             {"platform": "gnews", "external_id": "gnews:CBMiZZZ", "url": "https://news.google.com/rss/articles/CBMiZZZZZZZZZZZZZZZZZZ",
              "source_url": "https://neva.today", "title": "Погода на выходные - Нева Today", "text": "дожди"}]
    asyncio.run(links.fix_items(ctx, client, items))
    assert items[0]["url"] == "https://www.spbdnevnik.ru/news/2026-09-27/gzhi-114" and seen[0].endswith(f"/gn-resolve/{gid}")
    assert items[1]["url"].startswith(links.SEARCH_PREFIX) and "neva.today" in items[1]["url"]   # не про нас — без запроса
    assert len(seen) == 1
    from monitor.alerts.formatting import link_label
    assert link_label({"url": items[1]["url"]}) == "найти статью (поиск)"


def test_media_spread(ctx):
    from monitor.spread import spread

    for key, title in (("rss:spbdnevnik", "Петербургский дневник"), ("rss:fontanka", "Фонтанка")):
        ctx.db.upsert_source({"key": key, "kind": "rss", "handle": key, "title": title, "category": "media", "region": "SPB"})
    t0 = now_utc() - timedelta(hours=5)
    rows = [("rss:spbdnevnik", "a1", "ГЖИ выявила нарушения в доме на Ленинском проспекте, 114: управляющая компания "
             "получила предписание устранить протечки кровли и восстановить освещение подъездов", t0),
            ("rss:fontanka", "b1", "На Ленинском проспекте, 114 ГЖИ выявила нарушения: управляющей компании выдано "
             "предписание устранить протечки кровли и восстановить освещение", t0 + timedelta(hours=1)),
            ("rss:fontanka", "b2", "Погода: в Петербурге ожидаются дожди и ветер до конца недели", t0 + timedelta(hours=2))]
    ids = []
    for src, eid, text, t in rows:
        ctx.pipeline.ingest([{"platform": "rss", "external_id": eid, "url": f"https://site/{eid}", "title": text[:60],
                              "text": text, "published_at": iso(t)}], {"key": src, "category": "media", "region": "SPB"})
        ids.append(ctx.db.one("SELECT id FROM items WHERE external_id=?", (eid,))["id"])
    sp = spread(ctx, ctx.db.item(ids[0]))
    assert [r["source"] for r in sp["reprints"]] == ["Фонтанка"]
