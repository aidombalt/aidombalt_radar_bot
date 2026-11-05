"""Сборка 2026.10.23: кейс «яйца в дверь пенсионеров» (ЖК Виктория, Авиаторов Балтики 9к2, 21–23.10).

1. Хулиганство/травля без претензии к УК — не P2 «ответить от лица УК», а P3 в отчёт; «Судя по записи» ≠ суд.
2. «❌ Ложное»/«🟢 Решено» гасят перепосты той же истории, даже если в них назван наш адрес; закрытые карточки
   уходят из чатов Telegram и Max через заданное время.
3. Выключение/перезапуск из веб-интерфейса: коды выхода, метка для сторожа.
"""
import asyncio
import json

import httpx
import pytest

from monitor import power, rules
from monitor.alerts import cleanup
from monitor.alerts.commands import Actor, command
from monitor.alerts.max_api import MaxAPI
from monitor.alerts.render import card_for
from monitor.brain import core
from monitor.util import ago, iso, jload
from tests.test_max import FakeMax, callback

EGGS = ("Западное Мурино.\n\nУважаемые жители ЖК \"Виктория\", прошу вашей помощи.\nМои родители, пенсионеры преклонного "
        "возраста, живут по адресу: Авиаторов Балтики, 9к2. Уже около года примерно раз в две недели подростки забрасывают "
        "их входную дверь яйцами и с силой бьют по ней ногами.\n\nВсе происходит молниеносно: подбегают к двери, бросают "
        "яйца, наносят удары и сразу убегают.\nЭто происходит поздно вечером, пугает родителей и плохо сказывается на их "
        "самочувствии. Для фиксации происходящего пришлось установить камеру. Судя по записи, от подобных действий страдают "
        "не только мои родители, но и другие жители.")

# ровно то, что ответила локальная модель 23.10 (карточка #70632): P2, «безопасность», «несмотря на отсутствие обращения к УК»
LLM_EGGS = {"relevant": True, "relation": "our", "sentiment": -1, "tone": "жалоба",
            "topics": ["безопасность", "суд_прокуратура_надзор"], "addressees": [], "author_type": "житель",
            "call_to_action": True, "hazard": False, "virality": "средняя", "priority": "P2",
            "summary": "Жители ЖК Виктория сообщают о травле пожилых соседей подростками",
            "why": "негатив, адресованный жителям нашего ЖК, несмотря на отсутствие прямого обращения к УК",
            "action": "Проверить факт на объекте и ответить от лица УК в течение 2 часов", "reply_draft": "Уважаемые жители…"}

WATER = "ЖК Виктория, Авиаторов Балтики 9к2: третий день нет горячей воды, УК не отвечает, сколько можно терпеть"
LLM_WATER = {**LLM_EGGS, "topics": ["горячая_вода"], "call_to_action": False, "summary": "нет горячей воды",
             "why": "жалоба на нашу УК", "responsibility": "УК"}

SOURCES = {
    "devyatkino": {"key": "vk_wall:devyatkino_online", "title": "Девяткино онлайн | Мурино", "category": "public", "region": "LO"},
    "podslushano": {"key": "vk_wall:podslushano_v_murino", "title": "Подслушано в Мурино", "category": "public", "region": "LO"},
    "media": {"key": "rss:https://47news.ru/rss/", "title": "47news", "category": "media", "region": "LO"},
}
_n = [0]


def put(ctx, text, analysis, src="devyatkino"):
    _n[0] += 1
    s = SOURCES[src]
    ctx.db.upsert_source({"key": s["key"], "kind": s["key"].split(":")[0], "handle": s["key"], "title": s["title"],
                          "category": s["category"], "region": s["region"], "tier": "hot", "where": "server"})
    raw = {"platform": "vk_post", "external_id": f"vk:-1_{_n[0]}", "url": f"https://vk.com/wall-1_{_n[0]}",
           "author_id": f"vk:{_n[0]}", "author_name": "житель", "text": text, "published_at": iso()}
    ctx.pipeline.ingest([raw], s)
    iid = ctx.db.one("SELECT id FROM items WHERE external_id=?", (raw["external_id"],))["id"]
    ctx.pipeline.finalize(iid, dict(analysis), "local")
    return iid


def prio(ctx, iid):
    return ctx.db.item(iid)["priority"]


def risk(ctx, iid):
    return (jload(ctx.db.item(iid)["analysis_json"], {}) or {}).get("_risk") or {}


def alerted(ctx, iid):
    return bool(ctx.db.one("SELECT 1 FROM alerts WHERE item_id=?", (iid,)))


# ============================================================ 1. тема: не про работу УК
def test_sudya_is_not_a_court():
    assert rules.topic_hits("судя по записи, страдают и другие") == []
    assert rules.topic_hits("посуда и судьба") == []
    for t in ("подали в суд на ук", "решение суда", "будем судиться с управляющей", "мировой судья", "судебный иск"):
        assert rules.topic_hits(t) == ["суд_прокуратура_надзор"], t


def test_eggs_case_is_not_an_alert(ctx):
    iid = put(ctx, EGGS, LLM_EGGS)
    assert prio(ctx, iid) == "P3" and not alerted(ctx, iid)
    assert "хулиганство" in risk(ctx, iid)["combine"]
    assert "тема:суд_прокуратура_надзор" not in jload(ctx.db.item(iid)["tags"], [])
    r = put(ctx, EGGS + " ", {**LLM_EGGS, "responsibility": "полиция"}, "podslushano")
    assert prio(ctx, r) == "P3"
    ctx.pipeline.rules_analyze(r)                                 # и без ИИ
    assert prio(ctx, r) in ("P3", "P4") and not alerted(ctx, r)


@pytest.mark.parametrize("text", [
    "Подростки в ЖК Виктория на Авиаторов Балтики 9к2 сожгли кнопки, лифт не работает неделю, УК не реагирует!",
    "ЖК Виктория, Авиаторов Балтики 9к2: хулиганы ломают домофон, дверь в парадную не закрывается, охрана ничего не делает",
    "Хулиганы бьют стёкла в ЖК Виктория (Авиаторов Балтики 9к2) — когда управляющая компания поставит камеры?",
])
def test_claim_against_uk_keeps_it_urgent(ctx, text):
    iid = put(ctx, text, {**LLM_EGGS, "responsibility": "полиция"})
    assert prio(ctx, iid) in ("P1", "P2") and alerted(ctx, iid), risk(ctx, iid)


@pytest.mark.parametrize("text,label,claim", [
    ("УК обещала травлю тараканов в подвале, до сих пор не сделали", "", True),      # травля насекомых — тема УК
    ("в подвале травля крыс не проводится месяц", "", False),
    ("подростки курят в подъезде, а УК ничего не делает", "хулиганство", True),
    ("вандалы сломали домофон, неделю не работает", "вандализм", True),
    ("Компания молодёжи по ночам орёт под окнами у ЖК", "хулиганство", False),
    ("у нас конфликт с соседями из-за парковки", "конфликт соседей", False),
    ("Дверь в подъезд не закрывается, ходят посторонние", "", True),
    ("у двери подъезда стоит подозрительный мужчина", "", False),                    # место — не претензия
    ("плесень угрожает здоровью детей, УК молчит", "", True),
    ("Подростки бьют двери, охрана ЖК бездействует", "хулиганство", True),
    ("Хулиганы опять в подъезде. Куда смотрит управляйка?", "хулиганство", True),
    ("охрана объекта сообщила в полицию", "", False),
])
def test_subject_classifier_tricky_phrasings(text, label, claim):
    from monitor.subject import third_party, uk_claim
    assert (third_party(text), uk_claim(text)) == (label, claim)


def test_press_about_hooligans_is_not_capped(ctx):
    iid = put(ctx, EGGS, LLM_EGGS, "media")                       # СМИ о нашем ЖК — вопрос репутации, решает человек
    assert prio(ctx, iid) in ("P1", "P2")


def test_model_responsibility_lowers_only_without_uk_claim(ctx):
    noisy = "Жители ЖК Виктория на Авиаторов Балтики 9к2 просят соседа сверху не сверлить по ночам, сил больше нет"
    assert prio(ctx, put(ctx, noisy, {**LLM_EGGS, "responsibility": "жильцы_соседи"})) == "P3"
    assert prio(ctx, put(ctx, noisy + "!", {**LLM_EGGS, "responsibility": "УК"})) in ("P1", "P2")
    # модель сказала «полиция», но в тексте авария ЖКХ — тексту верим больше
    assert prio(ctx, put(ctx, WATER, {**LLM_WATER, "responsibility": "полиция"})) in ("P1", "P2")


def test_core_schema_asks_responsibility_before_priority():
    props = list(core.ANALYSIS_SCHEMA["properties"])
    assert props.index("responsibility") < props.index("priority")
    assert "responsibility" in core.ANALYSIS_SCHEMA["required"]
    assert core.normalize_analysis({"relevant": True, "priority": "P2"})["responsibility"] == "не_определено"
    assert core.normalize_analysis({"relevant": True, "priority": "P2", "responsibility": "полиция"})["responsibility"] == "полиция"
    assert "яйцами" in core.SYSTEM_PROMPT and core.SYSTEM_PROMPT.format(company="X")


# ============================================================ 2а. решение оператора держится за историю
def test_false_verdict_silences_reposts_with_our_address(ctx):
    root = put(ctx, WATER, LLM_WATER)
    assert prio(ctx, root) in ("P1", "P2") and alerted(ctx, root)
    ctx.alerts.ack(root, "leva protiv", "false_positive")
    copy = put(ctx, "Мурино!\n" + WATER + "\n#Мурино #ЖКВиктория", LLM_WATER, "podslushano")
    r = risk(ctx, copy)
    assert prio(ctx, copy) == "P3" and not alerted(ctx, copy)
    assert r["cap_kind"] == "same_case" and r["copy_of"] == root and r["copy_kind"] == "fp"
    assert "Ложное" in r["combine"]
    # другая проблема по тому же адресу — это не копия, будит как обычно
    other = put(ctx, "ЖК Виктория, Авиаторов Балтики 9к2: лифт не работает пятый день, УК молчит, пенсионеры не могут выйти",
                {**LLM_WATER, "topics": ["лифт"]})
    assert prio(ctx, other) in ("P1", "P2") and alerted(ctx, other)


def test_copy_with_new_claim_against_uk_is_not_silenced(ctx):
    root = put(ctx, EGGS, LLM_EGGS)
    ctx.alerts.ack(root, "leva protiv", "false_positive")
    copy = put(ctx, EGGS + " Управляющая компания на наши обращения не отвечает, охрана ничего не делает.", LLM_EGGS)
    assert prio(ctx, copy) in ("P1", "P2") and not risk(ctx, copy).get("copy_of")


def test_resolved_verdict_silences_copies_until_story_escalates(ctx):
    root = put(ctx, WATER, LLM_WATER)
    ctx.alerts.ack(root, "Оператор", "resolved")
    copy = put(ctx, WATER + " ", LLM_WATER, "podslushano")
    assert prio(ctx, copy) == "P3" and risk(ctx, copy)["copy_kind"] == "closed" and not alerted(ctx, copy)
    up = put(ctx, "Александр Юрьевич, помогите! " + WATER, {**LLM_WATER, "addressees": ["губернатор ЛО"]})
    assert prio(ctx, up) in ("P1", "P2") and alerted(ctx, up)    # дошло до губернатора — новый уровень
    again = put(ctx, "Опять! " + WATER, LLM_WATER)                 # «опять» — проблема повторилась, это новость
    assert prio(ctx, again) in ("P1", "P2") and not risk(ctx, again).get("copy_of")


def test_resolved_verdict_expires_sooner_than_false(ctx):
    root = put(ctx, WATER, LLM_WATER)
    ctx.alerts.ack(root, "Оператор", "resolved")
    old = iso(__import__("monitor.util", fromlist=["now_utc"]).now_utc() - __import__("datetime").timedelta(days=4))
    ctx.db.x("UPDATE items SET published_at=?, collected_at=? WHERE id=?", (old, old, root))
    ctx.cases.invalidate()
    late = put(ctx, WATER + " ", LLM_WATER)                         # через 4 дня те же слова — скорее новая авария
    assert prio(ctx, late) in ("P1", "P2") and not risk(ctx, late).get("copy_of")
    ctx.db.x("UPDATE items SET workflow='false_positive' WHERE id=?", (root,))   # а «Ложное» держится 30 дней
    ctx.db.x("INSERT INTO feedback(item_id,user,verdict,ts) VALUES(?,?,?,?)", (root, "Оператор", "false_positive", iso()))
    assert prio(ctx, put(ctx, WATER + "  ", LLM_WATER)) == "P3"


def test_marking_one_copy_false_collapses_open_twins(ctx):
    a = put(ctx, WATER, LLM_WATER)
    b = put(ctx, "Репост: " + WATER, LLM_WATER, "podslushano")
    assert alerted(ctx, a)
    ctx.alerts.ack(a, "Оператор", "false_positive")
    if prio(ctx, b) in ("P1", "P2"):                           # пришла раньше вердикта — сверщик опустит
        assert b in ctx.pipeline.reconcile_down()
    assert prio(ctx, b) == "P3"
    ctx.alerts.reopen(a, "Оператор")                           # «↩️ Вернуть» снимает и с копий
    assert risk(ctx, b).get("copy_kind") in (None, "white")


# ============================================================ 2б. уборка закрытых карточек из чатов
@pytest.fixture
def chats(ctx, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "test-max-token")
    monkeypatch.setenv("MAX_ADMIN_CHAT_ID", "-77")
    monkeypatch.setenv("TG_BOT_TOKEN", "123:test")
    ctx.settings.alerts.night_hold = False
    ctx.db.x("UPDATE users SET max_user_id='501' WHERE tg_id='111'")
    fake = FakeMax()
    ctx.alerts.max._api = MaxAPI("test-max-token", transport=httpx.MockTransport(fake.handler))
    tg = []

    def tg_handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        tg.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 20 + len(tg)}})

    ctx.alerts._http = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    fake.tg = tg
    return fake


def deletes(fake):
    return [c for c in fake.calls if c[0] == "DELETE" and c[1] == "/messages"], [m for m in fake.tg if m[0] == "deleteMessage"]


def age_verdict(ctx, iid, minutes):
    ctx.db.x("UPDATE feedback SET ts=? WHERE item_id=?", (ago(minutes=minutes), iid))


@pytest.mark.asyncio
async def test_false_card_leaves_both_chats_after_delay(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    assert ctx.db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (iid,))
    assert ctx.db.one("SELECT 1 FROM max_msgs WHERE item_id=? AND kind='card'", (iid,))
    await ctx.max_bot.handle(callback(iid, cid="cb-rep", action="rep"))     # ответ «Черновик» — тоже уйдёт
    await ctx.max_bot.handle(callback(iid, cid="cb-fp", action="fp"))
    await ctx.alerts.process()
    assert "🗑 уберу из чата через 30 мин" in card_for(ctx, iid).text_html
    assert await cleanup.sweep(ctx) == 0                      # рано: можно ещё нажать «↩️ Вернуть»
    age_verdict(ctx, iid, 31)
    n = await cleanup.sweep(ctx)
    mx_del, tg_del = deletes(chats)
    assert n >= 3 and len(tg_del) == 1 and len(mx_del) >= 2   # TG-карточка; Max — карточка и ответ «Черновик»
    for t in ("alert_msgs", "alert_pings", "max_msgs"):
        assert not ctx.db.one(f"SELECT 1 FROM {t} WHERE item_id=?", (iid,)), t
    assert await cleanup.sweep(ctx) == 0                      # повторно ничего не делает
    assert ctx.db.item(iid)["workflow"] == "false_positive"   # публикация на месте — в GUI и отчётах


@pytest.mark.asyncio
async def test_resolved_waits_longer_and_undo_cancels(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    ctx.alerts.ack(iid, "Оператор", "resolved")
    age_verdict(ctx, iid, 60)
    assert await cleanup.sweep(ctx) == 0                      # «Решено» — 120 минут
    age_verdict(ctx, iid, 121)
    ctx.alerts.reopen(iid, "Оператор")                        # успели вернуть — не трогаем
    assert await cleanup.sweep(ctx) == 0 and ctx.db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (iid,))


@pytest.mark.asyncio
async def test_old_telegram_card_and_refusals_are_not_retried_forever(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    ctx.db.x("UPDATE alert_msgs SET sent_at=? WHERE item_id=?", (ago(hours=50), iid))   # Telegram: >48 ч не удалить
    chats.errors[("DELETE", "/messages")] = [(400, "message.not.found")]
    ctx.alerts.ack(iid, "Оператор", "false_positive")
    age_verdict(ctx, iid, 40)
    await cleanup.sweep(ctx)
    assert not deletes(chats)[1]                              # к Telegram даже не обращались
    assert not ctx.db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (iid,))
    assert not ctx.db.one("SELECT 1 FROM max_msgs WHERE item_id=?", (iid,))


@pytest.mark.asyncio
async def test_network_failure_backs_off_then_gives_up(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    chats.errors[("DELETE", "/messages")] = [(503, "service.unavailable")] * 20
    ctx.alerts.ack(iid, "Оператор", "false_positive")
    age_verdict(ctx, iid, 40)
    await cleanup.sweep(ctx)
    assert ctx.db.one("SELECT 1 FROM max_msgs WHERE item_id=?", (iid,))     # отложено
    assert cleanup.due(ctx) == []                                           # с паузой, не каждую минуту
    for _ in range(cleanup.MAX_TRIES):
        ctx.db.x("UPDATE kv SET value=json_set(value, '$.next', ?) WHERE key=?", (ago(minutes=1), f"{cleanup.TRY_KEY}{iid}"))
        await cleanup.sweep(ctx)
    assert not ctx.db.one("SELECT 1 FROM max_msgs WHERE item_id=?", (iid,))  # сдались, карточка осталась свёрнутой
    assert ctx.db.one("SELECT 1 FROM events WHERE message LIKE ?", (f"#{iid}: карточку не удалось убрать%",))


@pytest.mark.asyncio
async def test_auto_dropped_card_is_cleaned_and_reraise_cancels(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    ctx.db.x("UPDATE items SET priority='P3' WHERE id=?", (iid,))
    ctx.alerts.schedule_refresh(iid, "dropped", "по новым правилам")
    ctx.db.x("UPDATE kv SET value=? WHERE key=?", (json.dumps(ago(minutes=31)), f"{cleanup.DROPPED_KEY}{iid}"))
    ctx.db.x("UPDATE items SET priority='P2' WHERE id=?", (iid,))
    ctx.alerts.raise_alert(iid, "P2", update=True)            # снова срочно — не убираем
    assert await cleanup.sweep(ctx) == 0
    ctx.db.x("UPDATE items SET priority='P3' WHERE id=?", (iid,))
    ctx.alerts.queue_refresh(iid, "dropped")
    ctx.db.x("UPDATE kv SET value=? WHERE key=?", (json.dumps(ago(minutes=31)), f"{cleanup.DROPPED_KEY}{iid}"))
    assert await cleanup.sweep(ctx) >= 2


@pytest.mark.asyncio
async def test_undo_after_cleanup_brings_the_card_back(ctx, chats):
    iid = put(ctx, WATER, LLM_WATER)
    await ctx.alerts.process()
    ctx.alerts.ack(iid, "Оператор", "false_positive")
    age_verdict(ctx, iid, 31)
    await cleanup.sweep(ctx)
    ctx.alerts.reopen(iid, "GUI")
    rows = ctx.db.q("SELECT dedup_key FROM outbox WHERE item_id=? AND status='pending'", (iid,))
    assert any(r["dedup_key"].startswith("tg:") for r in rows) and any(r["dedup_key"].startswith("max:") for r in rows)


@pytest.mark.asyncio
async def test_cleanup_settings_via_bot(ctx):
    admin = Actor("111", "Оператор", "admin", "Telegram")
    assert "сохранена" in (await command(ctx, "set", "alerts.cleanup_false_min 0", admin))[0].text_html
    assert ctx.settings.alerts.cleanup_false_min == 0
    assert "от 0" in (await command(ctx, "set", "alerts.cleanup_resolved_min -5", admin))[0].text_html
    assert cleanup.note_line(ctx, 1, "false_positive") == ""
    assert "2 ч" in cleanup.note_line(ctx, 1, "resolved")


# ============================================================ 3. питание
def test_power_stop_and_restart(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(power, "STOP_MARKER", tmp_path / "stopped_by_user")
    assert power.request(ctx, "stop", "GUI · 10.0.0.5") == power.EXIT_STOP == ctx.exit_code
    assert power.STOP_MARKER.exists()
    assert ctx.db.one("SELECT 1 FROM outbox WHERE dedup_key LIKE 'adm:power:%'")         # команда узнает
    last = power.on_start(ctx)
    assert last["by"] == "GUI · 10.0.0.5" and last["action"] == "stop" and not power.STOP_MARKER.exists()
    assert power.request(ctx, "restart") == power.EXIT_RESTART == 75 and not power.STOP_MARKER.exists()


def test_supervisor_detection(monkeypatch):
    for k in ("RADAR_SUPERVISOR", "RADAR_LAUNCHER", "INVOCATION_ID"):
        monkeypatch.delenv(k, raising=False)
    assert power.supervisor() == "" and not power.can_restart() and power.can_stop()[0]
    monkeypatch.setenv("RADAR_LAUNCHER", "bat")
    assert power.supervisor() == "bat" and power.can_restart()
    monkeypatch.setenv("RADAR_SUPERVISOR", "docker")
    assert power.supervisor() == "docker" and not power.can_stop()[0]


def test_launchers_understand_exit_codes():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    bat = (root / "start_radar.bat").read_bytes()
    assert b"\r\n" in bat and b'"%RC%"=="0" goto stopped' in bat and b'"%RC%"=="75" goto restart' in bat
    assert b"RADAR_LAUNCHER=bat" in bat
    assert "Restart=on-failure" in (root / "deploy/radar.service").read_text("utf-8")
    assert "stopped_by_user" in (root / "deploy/radar-watchdog.sh").read_text("utf-8")
    assert "RADAR_SUPERVISOR=docker" in (root / "deploy/Dockerfile").read_text("utf-8")


# ============================================================ 23.10 16:30: СК о «доме на проспекте Обуховской Обороны»
SK = ("Председателю СК России будет представлен доклад о нарушении жилищных прав граждан в Санкт-Петербурге\n\n"
      "В социальных медиа размещена публикация, в которой сообщается о том, что жители многоквартирного дома на проспекте "
      "Обуховской Обороны в Санкт-Петербурге несколько лет сообщают об аварийном состоянии здания: разрушаются потолки и "
      "межэтажные перекрытия, имеются протечки, неисправна вентиляция, неудовлетворительно содержатся места общего "
      "пользования. Жильцы неоднократно обращались в управляющую компанию, однако необходимые ремонтные работы до "
      "настоящего времени не выполнены. Жители выражают опасения в связи с возможной угрозой обрушения конструкций и просят "
      "принять меры реагирования.\n\nВ целях установления всех обстоятельств произошедшего следственными органами ГСУ СК "
      "России по Санкт-Петербургу возбуждено уголовное дело. Ранее следователями дважды возбуждались по данным фактам "
      "уголовные дела, которые отменялись надзорным органом.\n\nГлава ведомства Бастрыкин А.И. поручил и.о. руководителя "
      "ГСУ СК России по Санкт-Петербургу Козлову В.П. представить доклад об установленных обстоятельствах, а также о мерах, "
      "принимаемых для защиты прав граждан.")
# что ответила модель (карточка 16:33): «напрямую касается нашего ЖК (ЖК Дом-Мегалит на Неве)»
LLM_SK = {"relevant": True, "relation": "our", "sentiment": -2, "tone": "жалоба",
          "topics": ["протечки_кровля", "безопасность", "суд_прокуратура_надзор"], "addressees": ["Следственный комитет"],
          "author_type": "СМИ", "call_to_action": False, "hazard": True, "priority": "P2", "responsibility": "УК",
          "summary": "В СМИ сообщение об аварийном доме на проспекте Обуховской Обороны, возбуждено уголовное дело",
          "why": "Публикация напрямую касается нашего ЖК (ЖК Дом-Мегалит на Неве) и адресована в СК",
          "action": "Проверить факт на объекте и ответить от лица УК", "reply_draft": "Уважаемые жители, УК «Балтийский Дом»…"}
SOURCES["sk"] = {"key": "tg_channel:infocentrskrf", "title": "infocentrskrf", "category": "official", "region": "RU"}


def test_street_without_house_is_not_our_object(ctx):
    iid = put(ctx, SK, LLM_SK, "sk")
    a = jload(ctx.db.item(iid)["analysis_json"], {})
    assert prio(ctx, iid) == "P3" and not alerted(ctx, iid)
    assert "Обуховской Обороны без номера дома" in a["_risk"]["combine"]
    assert a["relation"] == "cluster" and a["_relation_fixed"] == "our→cluster"      # модель не «назначает» наш ЖК
    assert "Мегалит" not in a["why"] and a["reply_draft"] == "" and "только улица" in a["why"]


def test_street_hint_does_not_name_our_zhk(ctx):
    h = ctx.matcher.hint(ctx.matcher.match(SK, "", "official", "SPB"))
    assert "Мегалит" not in h and "номер дома НЕ назван" in h and "relation=cluster" in h and "195" in h
    assert "Мегалит" in ctx.matcher.hint(ctx.matcher.match("Обуховской Обороны, 195 — протечки", "", "public", "SPB"))
    assert "Обуховской Обороны" in core.SYSTEM_PROMPT and core.CORE_VERSION >= "2026.10.23b"


def test_our_house_number_or_dense_street_still_alerts(ctx):
    ours = put(ctx, SK.replace("на проспекте Обуховской Обороны", "на проспекте Обуховской Обороны, 195"), LLM_SK, "sk")
    a = jload(ctx.db.item(ours)["analysis_json"], {})
    assert prio(ctx, ours) in ("P1", "P2") and alerted(ctx, ours) and a["relation"] == "our" and "Мегалит" in a["why"]
    ctx.settings.risk.street_dense = ["Обуховской Обороны"]           # улица, где «почти все дома наши» — по настройке
    assert prio(ctx, put(ctx, SK + " ", LLM_SK, "sk")) == "P2"


def test_already_sent_street_alert_is_collapsed_by_reconciler(ctx):
    iid = put(ctx, SK, LLM_SK, "sk")
    ctx.db.x("UPDATE items SET priority='P2' WHERE id=?", (iid,))     # как пришло до обновления
    ctx.db.x("INSERT INTO alerts(item_id, priority, created_at, last_notified_at) VALUES(?,?,?,?)", (iid, "P2", iso(), iso()))
    assert iid in ctx.pipeline.reconcile_down() and prio(ctx, iid) == "P3"
    assert ctx.db.one("SELECT 1 FROM kv WHERE key=?", (f"{cleanup.DROPPED_KEY}{iid}",))   # свернётся и уйдёт из чата


def test_relation_guard_keeps_real_and_watched_cases(ctx):
    fixed = ctx.pipeline.guard_relation({"platform": "tg", "text": "Мурино, УК опять"}, {"hits": [], "clusters": ["murino"]},
                                        {"relation": "our", "priority": "P2", "why": "наш ЖК"})
    assert fixed["relation"] == "cluster" and fixed["priority"] == "P3"
    watched = {"hits": [], "clusters": [], "reason": "автор на контроле"}
    assert ctx.pipeline.guard_relation({"platform": "tg"}, watched, {"relation": "our"})["relation"] == "our"
    m = ctx.matcher.match(WATER).to_json()
    assert ctx.pipeline.guard_relation({"platform": "tg"}, m, {"relation": "our"})["relation"] == "our"
    assert ctx.pipeline.guard_relation({"platform": "manual"}, {"hits": []}, {"relation": "our"})["relation"] == "our"


# ============================================================ 03.11: «ЖК Солнечный город» — общий ЖК + комментарий под «Ложным»
SG_POST = ("⚠️ Жители ЖК «Солнечный город» жалуются на неприятный запах воды из-под крана.\nПо словам соседей, с недавнего "
           "времени вода имеет выраженный неприятный запах. Жители обеспокоены её качеством и интересуются, с чем может быть "
           "связана проблема.\nУ вас тоже появился неприятный запах воды? Делитесь в комментариях.")
SG_COMMENT = "[club91135910|Водоканал Санкт-Петербурга] Ждем Вас срочно с проверкой в ЖК Солнечный город"
# что ответила модель (карточка 08:59): «напрямую касается ЖК Солнечный город, который управляется УК Цивилизация»
LLM_SG = {"relevant": True, "relation": "our", "sentiment": -1, "tone": "жалоба", "topics": ["прочее"],
          "addressees": ["Водоканал"], "author_type": "житель", "call_to_action": True, "hazard": False, "priority": "P2",
          "responsibility": "УК", "summary": "призыв к Водоканалу проверить качество воды в ЖК Солнечный город",
          "why": "Публикация напрямую касается ЖК «Солнечный город», который управляется УК «Цивилизация»",
          "action": "Проверить факт на объекте и ответить от лица УК", "reply_draft": "Уважаемые жители…"}
SOURCES["sg"] = {"key": "vk_wall:zhizn_v_sg", "title": "Жизнь в Солнечном городе СПб", "category": "public", "region": "SPB"}


def put_comment(ctx, post_id, text, analysis):
    _n[0] += 1
    post = ctx.db.item(post_id)
    s = SOURCES["sg"]
    raw = {"platform": "vk_comment", "external_id": f"{post['external_id']}_{_n[0]}", "url": post["url"] + f"?reply={_n[0]}",
           "author_id": f"vk:{_n[0]}", "author_name": "Elena D", "text": text, "published_at": iso(),
           "parent_external_id": post["external_id"], "parent_text": post["text"]}
    ctx.pipeline.ingest([raw], s)
    iid = ctx.db.one("SELECT id FROM items WHERE external_id=?", (raw["external_id"],))["id"]
    ctx.pipeline.finalize(iid, dict(analysis), "local")
    return iid


def test_shared_zhk_name_without_house_is_not_ours(ctx):
    post = put(ctx, SG_POST, LLM_SG, "sg")
    it = ctx.db.item(post)
    a = jload(it["analysis_json"], {})
    assert prio(ctx, post) == "P3" and not alerted(ctx, post)
    assert "есть дома других УК" in a["why"]                                       # страж исправил вывод модели
    m = jload(it["match_json"], {})
    cap, why = ctx.pipeline.evidence_cap(dict(it), m, {"relation": "our", "sentiment": -2})
    assert cap == "P3" and "есть дома других УК" in why                             # и потолок по тексту — P3
    assert "Цивилизация" not in a["why"] and a["reply_draft"] == "" and a["relation"] == "cluster"
    tags = jload(it["tags"], [])
    assert "ЖК:Солнечный Город" in tags and not any(t.startswith("УК:") for t in tags)   # чья УК — неизвестно
    hint = ctx.matcher.hint(ctx.matcher.match(SG_POST, "", "public", "SPB"))
    assert "ДРУГИХ УК" in hint and "Цивилизация" not in hint


@pytest.mark.parametrize("text", [
    "ЖК Солнечный город, Буденного 19к1 — третий день вода с запахом, УК не отвечает",        # наш дом назван
    "Во всём ЖК Солнечный город вода пахнет тухлыми яйцами, УК молчит",                       # весь ЖК — и наши дома
    "УК Цивилизация, в ЖК Солнечный город вода с запахом, когда проверите?",                    # наша УК названа
])
def test_shared_zhk_with_our_house_or_uk_stays_urgent(ctx, text):
    iid = put(ctx, text, {**LLM_SG, "topics": ["холодная_вода"]}, "sg")
    assert prio(ctx, iid) in ("P1", "P2") and alerted(ctx, iid), risk(ctx, iid)


def test_comment_under_false_post_inherits_verdict(ctx):
    post = put(ctx, SG_POST, LLM_SG, "sg")
    ctx.alerts.ack(post, "Оператор", "false_positive")
    c = put_comment(ctx, post, SG_COMMENT, LLM_SG)
    r = risk(ctx, c)
    assert prio(ctx, c) == "P3" and not alerted(ctx, c)
    assert r["copy_of"] == post and r["copy_kind"] == "fp"
    assert ctx.db.item(c)["text"].startswith("Водоканал Санкт-Петербурга Ждем")              # без разметки ВК
    # то же для ЖК, где все дома наши: срабатывает именно наследование по ветке
    vp = put(ctx, SG_POST.replace("Солнечный город", "Виктория"), LLM_SG, "sg")
    ctx.alerts.ack(vp, "Оператор", "false_positive")
    vc = put_comment(ctx, vp, SG_COMMENT.replace("Солнечный город", "Виктория"), LLM_SG)
    assert prio(ctx, vc) == "P3" and risk(ctx, vc)["cap_kind"] == "same_case" and risk(ctx, vc)["copy_of"] == vp
    # комментатор назвал наш дом — это уже его история, будит
    own = put_comment(ctx, vp, "У нас на Авиаторов Балтики 9к1 тоже вода пахнет, УК молчит", LLM_SG)
    assert prio(ctx, own) in ("P1", "P2") and not risk(ctx, own).get("copy_of")


def test_comment_under_resolved_post_unless_problem_returns(ctx):
    vp = put(ctx, WATER, LLM_WATER)
    ctx.alerts.ack(vp, "Оператор", "resolved")
    quiet = put_comment(ctx, vp, "Да, у нас тоже так было, спасибо что починили", LLM_WATER)
    assert prio(ctx, quiet) in ("P3", "P4") and not alerted(ctx, quiet)
    again = put_comment(ctx, vp, "Опять нет горячей воды, ЖК Виктория, Авиаторов Балтики 9к2!", LLM_WATER)
    assert prio(ctx, again) in ("P1", "P2") and alerted(ctx, again)


def test_false_on_post_collapses_comment_that_came_earlier(ctx):
    vp = put(ctx, SG_POST.replace("Солнечный город", "Виктория"), LLM_SG, "sg")
    vc = put_comment(ctx, vp, SG_COMMENT.replace("Солнечный город", "Виктория"), LLM_SG)
    assert alerted(ctx, vc)
    ctx.alerts.ack(vp, "Оператор", "false_positive")
    assert vc in ctx.pipeline.reconcile_down() and prio(ctx, vc) == "P3"


def test_memory_tells_model_about_thread_verdict(ctx):
    post = put(ctx, SG_POST, LLM_SG, "sg")
    ctx.alerts.ack(post, "Оператор", "false_positive")
    c = put_comment(ctx, post, SG_COMMENT, LLM_SG)
    pkg = ctx.memory.package(ctx.db.item(c), "")
    assert "ВЕТКА" in pkg["memory"] and "Ложное" in pkg["memory"]


def test_shared_flag_checkbox_writes_yaml(tmp_path):
    from monitor import entities
    src = tmp_path / "entities.yaml"
    src.write_text('zhk:\n  - {key: sungorod, name: "Солнечный Город", aliases: ["Солнечный Город"], verified: "2026-10-09"}\n',
                   "utf-8")
    assert entities.set_zhk_shared("sungorod", True, "есть дома других УК", src)
    assert 'shared: true, shared_note: "есть дома других УК"}' in src.read_text("utf-8")
    assert entities.set_zhk_shared("sungorod", False, path=src) and "shared" not in src.read_text("utf-8")
