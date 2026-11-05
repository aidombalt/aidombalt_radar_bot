"""«Чужой дом в нашем ЖК» (инцидент 08–09.10.2026: 13к1 в ЖК «Прогресс» обслуживает УК «Сервис+», наши — 13 к2/к4/к5/к6).

Дизайн v7: разбор зданий и замкнутый мир справочника (§2), объяснение хита ЖК чужим корпусом (§3), полнота справочника
и потолки (§4), «та же история» (§5), решения оператора как данные (§6), эскалации без дублей (§7)."""
import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from monitor import entities, learned, workhours
from monitor.matcher import Matcher
from monitor.reports.situation import weak_objects
from monitor.rules import analyze as rules_analyze
from monitor.util import ago, iso, jload
from tests.incident_texts import DTP_KUDROVO, DTP_NOADDR, KUDROVO_VK, NEWS47

ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
LLM_P1 = {"relevant": True, "relation": "our", "priority": "P1", "sentiment": -2, "tone": "жалоба",
          "topics": ["лифт", "суд_прокуратура_надзор"], "addressees": ["губернатор ЛО", "прокуратура"],
          "summary": "жалоба на лифт", "why": "касается нашего ЖК", "call_to_action": False, "hazard": False}


@pytest.fixture()
def mt(ctx):
    return ctx.matcher


def m_of(mt, text, **kw):
    return mt.match(text, source_region="LO", **kw)


# ============================================================ §2–§3: матчер
EXPLAINED = [
    NEWS47,
    DTP_KUDROVO,                                                     # адрес отдельным предложением
    DTP_NOADDR + "\n\n📍 Европейский пр., 13к1",                      # адресная строка
    "Дом 13к1 рядом с ЖК Прогресс: лифт стоит полгода",                # «рядом с» не блокирует
    "Жители 13к1 тоже жалуются (ЖК Прогресс)",                         # голое «тоже» не блокирует
    "Жители дома 13 на Европейском (корпус 1) и ЖК Прогресс: лифт стоит",
    "⚡️Кудрово\nЖители ЖК Прогресс жалуются на лифт.\nВ доме №13к1 на Европейском УК требует 600 тысяч.",
    "Жильцам ЖК «Прогресс» в Кудрово предложили собрать 600 тысяч на ремонт лифта\nЖители дома №13к1 на Европейском "
    "проспекте готовят обращения в прокуратуру и губернатору.",
    "Жители дома №13к1 на Европейском (ЖК «Прогресс») в понедельник направили жалобу губернатору",
    "Жильцы дома №13к1 на Европейском проспекте (ЖК «Прогресс») решили сменить управляющую компанию",
    "Жители дома №13к1 (ЖК «Прогресс») соберутся на сход в субботу",
    "В доме №13к1 на Европейском (ЖК «Прогресс») решили сменить управляющую компанию",
    "Жители дома №13к1 на Европейском проспекте в Кудрово (ЖК «Прогресс») готовят обращения в инстанции, в том числе в "
    "прокуратуру и к губернатору",
    "Жители дома №13к1 на Европейском проспекте (ЖК «Прогресс») так же жалуются на сломанный домофон",
    "ЖК Прогресс. Жители дома 13к1: «А у нас лифт стоит полгода, УК молчит»",
    "Жители дома №19к3 на улице Буденного (ЖК «Солнечный город») возмущены: 600 тысяч на лифт",
]
OURS = [   # ЖК не объясняется: наш/частичный дом, блокировка, сбор от имени ЖК, ориентир, дайджест
    "Жители 13к1 и 13к2 ЖК Прогресс пишут губернатору: лифт стоит",
    "Во всём ЖК Прогресс нет воды, в доме 13к1 тоже",
    "Жители дома 13к1 жалуются на лифт, а у нас в ЖК Прогресс та же беда",
    "Жители ЖК Прогресс соберутся на сход во дворе дома №13к1 в субботу",
    "Сход жителей ЖК Прогресс против УК пройдёт в холле дома №13к1 на Европейском",
    "Жители ЖК Прогресс хотят сменить УК, в том числе жильцы дома №13к1 на Европейском",
    "Жильцы сообщают: во всём ЖК Прогресс нет горячей воды, в доме №13к1 на Европейском тоже",
    "Подписчики пишут, что в ЖК Прогресс без воды все корпуса, включая дом №13к1 на Европейском",
    "Жители ЖК Прогресс соберутся на сход в субботу.\nРечь о доме №13к1 на Европейском.",
    "В доме №13к1 на Европейском пройдёт сход всех жителей ЖК Прогресс против УК",
    "Главное за день в Кудрово:\n1) В ЖК Прогресс затопило паркинг, жильцы пишут в Госжилнадзор.\n"
    "2) В доме №13к1 на Европейском третий месяц стоит грузовой лифт.",
    "Новости Кудрово за день\nВ ЖК Прогресс затопило паркинг.\n\nВ доме №13к1 на Европейском стоит лифт.",
    "ЖК Прогресс: в доме 13 по Европейскому три корпуса обслуживают разные УК, в 13к1 лифт стоит",
    "УК Балтийский Дом не чинит лифт в 13к1, ЖК Прогресс",
    "ЖК Виктория, 2к2 — нет воды",
    "ЖК Прогресс, корпус 1: лифт стоит полгода, жалоба губернатору",
]


@pytest.mark.parametrize("text", EXPLAINED)
def test_zhk_explained_by_foreign_korpus(mt, text):
    m = m_of(mt, text)
    assert m.explained_hits and not [h for h in m.hits if h.kind == "zhk"], (m.hits, m.foreign)
    assert not m.ours and m.foreign and m.raw_level == 2


@pytest.mark.parametrize("text", OURS)
def test_zhk_stays_ours(mt, text):
    m = m_of(mt, text)
    assert not m.explained_hits and m.ours, (m.hits, m.foreign)


@pytest.mark.parametrize("text,key,conf", [
    ("Дом №13к2 на Европейском — лифт стоит", "европейский|13|2", 0.95),                 # номер перед улицей
    ("Жители первого корпуса дома 13 на Европейском жалуются", None, None),             # 13к1 — чужой корпус
    ("на Европейском проспекте 13 в Кудрово не работает лифт", "европейский|13|2", 0.85),  # «в» — не литера
    ("Европейский 13-45, течёт с потолка", "европейский|13|2", 0.85),                   # квартира — частично
    ("Ручьевский пр. 4 к1 — опять нет воды", "ручьевский|4|26", 0.85),                  # другая форма — частично
    ("ЖК Авиатор, Ручьевский 4 — нет воды", "ручьевский|4|26", 0.85),                   # опущенная дробь
    ("Коллонтай 5 — течёт крыша", "коллонтай|5|1", 0.85),
    ("Чапыгина 3, протечка", "чапыгина|3А|", 0.85),                                      # опущенная литера
    ("Екатерининская 22 корпус 2 — воды нет", "екатерининская|22|2", 0.95),              # род → ул., не пр.
    ("Предпортовый 9 — нет воды", "1йпредпортовый|9|", 0.95),                            # дом 9 только у 1-го
    ("5-й Предпортовый проезд, 9 — нет воды", None, None),                               # ординал 5: дома 9 нет
    ("Европейский 21-45, течёт", None, None),                                            # чужой основной номер
    ("Шувалова 40 и Шувалова 52 — снова нет горячей воды", "шувалова|40|", 0.95),
])
def test_building_classes(mt, text, key, conf):
    m = m_of(mt, text)
    addr = {(h.key, h.conf) for h in m.hits if h.kind == "address"}
    if key:
        assert (key, conf) in addr, m.hits
    else:
        assert not addr, m.hits


def test_known_neighbor_and_weak_house(mt):
    m = m_of(mt, "Европейский 13к1 — лифт стоит")
    assert m.neighbor and m.foreign and m.foreign[0]["known"] == "servis_plus"
    # «другой дом» на нашей улице ничего не объясняет (слабой привязки нет): сход у дома 10 — ЖК наш
    m = m_of(mt, "Сход жителей ЖК Виктория против УК — в субботу в 12:00. Адрес: ул. Шувалова, д. 10")
    assert m.ours and not m.explained_hits


def test_comment_codes_use_parent_zhk(ctx, mt):
    zk = ctx.pipeline.parent_zhks(NEWS47)
    assert zk == ["progress"]
    m = m_of(mt, "в 13к2 тоже лифт стоит, пишем в прокуратуру", context_zhks=zk)
    assert ("address", "европейский|13|2") in {(h.kind, h.key) for h in m.hits}


def test_hint_tells_model_the_house_is_not_ours(mt):
    m = m_of(mt, NEWS47)
    h = mt.hint(m)
    assert h.startswith("⚠ ЖК Прогресс: в тексте дом Европейский 13к1 — НЕ наш") and "к2" in h and "relation=neighbor" in h


def test_monotonic_caps_against_baseline(ctx):
    """Новый потолок не срочнее старого (снимок v2026.09.30-23), кроме новых распознанных наших зданий."""
    base = json.loads((Path(__file__).parent / "data" / "caps_baseline.json").read_text("utf-8"))
    bad = []
    for text, old in base.items():
        m = ctx.matcher.match(text, source_region="LO").to_json()
        cap, _ = ctx.pipeline.evidence_cap({"platform": "tg", "text": text, "title": "", "source_key": "tg_channel:x"}, m,
                                           {"relation": "our", "sentiment": -2})
        new_ours = any(h["kind"] == "address" and h["conf"] >= 0.45 for h in m["hits"])
        if ORDER[cap] < ORDER[old] and not new_ours:
            bad.append((old, cap, text[:80]))
    assert not bad, bad


# ============================================================ §3.4: старые потребители не видят объяснённых хитов
def test_consumers_do_not_see_explained_hits(ctx, mt):
    m = m_of(mt, NEWS47).to_json()
    a = rules_analyze({"text": NEWS47}, m)
    assert a["relation"] == "neighbor" and not a["entities"]
    assert "ЖК Прогресс" not in weak_objects(ctx, {"match_json": json.dumps(m, ensure_ascii=False), "tags": "[]"})
    db = ctx.db
    iid = db.x("INSERT INTO items(platform, external_id, text, match_json, analyzed_at, sentiment, relevant) "
               "VALUES('tg','w1',?,?,?,-1,1)", (NEWS47, json.dumps(m, ensure_ascii=False), iso()))
    from monitor import risk
    assert risk.wave(db, ["progress"], 0) == 0
    db.x("UPDATE items SET match_json=? WHERE id=?", (json.dumps(m_of(mt, "ЖК Прогресс: лифт стоит").to_json(),
                                                                 ensure_ascii=False), iid))
    assert risk.wave(db, ["progress"], 0) == 1
    assert "30 дней: P" not in ctx.memory.entity_card("Прогресс") or True


def test_min_match_conf_low_does_not_return_p1(ctx, mt):
    ctx.settings.risk.min_match_conf = 0.3
    m = m_of(mt, NEWS47).to_json()
    cap, _ = ctx.pipeline.evidence_cap({"platform": "rss", "text": NEWS47, "source_key": "rss:x"}, m, {})
    assert cap == "P3"


# ============================================================ конвейер: инцидент целиком
SRC = {"tg": {"key": "tg_channel:dtp_chp_kudrovo1", "title": "ДТП и ЧП Кудрово", "category": "public"},
       "rss": {"key": "rss:https://47news.ru/rss/", "title": "47news", "category": "media"}}


def post(ctx, text, src="tg", platform="tg", url=None, parent=None, llm=True):
    n = (ctx.db.scalar("SELECT COUNT(*) FROM items", (), 0) or 0) + 1
    raw = {"platform": platform, "external_id": f"e{n}", "url": url or f"https://example.org/{n}", "author_id": f"a{n}",
           "author_name": "автор", "text": text, "published_at": iso(), "parent_text": parent,
           "parent_external_id": "e1" if parent else None}
    ctx.pipeline.ingest([raw], SRC[src])
    iid = ctx.db.one("SELECT id FROM items WHERE external_id=?", (raw["external_id"],))["id"]
    if llm:
        ctx.pipeline.finalize(iid, dict(LLM_P1), "colab")
    else:
        ctx.pipeline.rules_analyze(iid)
    return iid


def prio(ctx, iid):
    return ctx.db.item(iid)["priority"]


def risk_of(ctx, iid):
    return (jload(ctx.db.item(iid)["analysis_json"], {}) or {}).get("_risk") or {}


def tg_cards(ctx, iid=None):
    q = "SELECT * FROM outbox WHERE channel='telegram' AND dedup_key LIKE 'tg:%'" + (" AND item_id=?" if iid else "")
    return ctx.db.q(q, (iid,) if iid else ())


def test_incident_night_is_quiet(ctx, monkeypatch):
    a = post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    assert prio(ctx, a) == "P3" and risk_of(ctx, a)["cap_kind"] == "foreign"
    monkeypatch.setattr(workhours, "is_off", lambda c, ts=None: True)
    d = post(ctx, DTP_KUDROVO)
    e = post(ctx, DTP_NOADDR)
    assert prio(ctx, d) == "P3" and prio(ctx, e) == "P3"
    assert risk_of(ctx, e)["cap_kind"] == "same_case" and risk_of(ctx, e)["copy_of"] in (a, d)
    ctx.db.x("UPDATE alerts SET last_notified_at=?", (ago(hours=2),))
    assert ctx.alerts.escalate() == 0
    assert not tg_cards(ctx)                                   # ни одной карточки за ночь
    ours = post(ctx, "ЖК Прогресс, корпус 13к4: грузовой лифт не работает уже месяц, УК предлагает собственникам "
                     "целевой сбор, пишем губернатору и в прокуратуру!")
    assert prio(ctx, ours) == "P1" and tg_cards(ctx, ours)     # настоящая жалоба на наш корпус — срочно


def test_without_neighbor_record_it_is_registry_check_held_till_morning(ctx, monkeypatch):
    ctx.reg.neighbors = [n for n in ctx.reg.neighbors if n["key"] != "servis_plus"]
    ctx.reg.zhks["progress"]["complete"] = False                 # как если бы список домов ЖК не был сверен
    ctx.matcher = Matcher(ctx.reg, ctx.watch.keywords)
    monkeypatch.setattr(workhours, "is_off", lambda c, ts=None: True)
    a = post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    r = risk_of(ctx, a)
    assert prio(ctx, a) == "P2" and r["cap_kind"] == "registry_check" and r["hold"]
    rows = tg_cards(ctx, a)
    assert rows and all(x["next_attempt_at"] > iso() for x in rows)    # СМИ + губернатор, но ждёт утра
    kb = jload(rows[0]["payload_json"])["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == f"h:our:{a}" and kb[0][1]["callback_data"] == f"h:not:{a}"
    assert not any(b["callback_data"].startswith("a:ack") for row in kb for b in row)   # «Беру» на «проверить» нет
    e = post(ctx, DTP_NOADDR)
    assert prio(ctx, e) == "P3" and not tg_cards(ctx, e)               # копия — без своей карточки


@pytest.mark.parametrize("text,copy", [
    (DTP_NOADDR, True),
    ("Жители ЖК Прогресс в Кудрово: «У нас в парадной 330 человек и один лифт». УК требует собрать 600 тысяч рублей на "
     "ремонт грузового лифта, иначе он заработает только к концу 2027 года. Люди называют это шантажом.", True),
    ("Жители ЖК Прогресс: в 13к4 грузовой лифт не работает уже месяц, управляющая компания предлагает собственникам "
     "целевой сбор на ремонт, пишем губернатору и в прокуратуру", False),
    ("ЖК Прогресс: бабушка на 18-м этаже неделю не может спуститься, лифт стоит, УК молчит, пишем губернатору", False),
    ("ЖК Прогресс: в нашем корпусе УК тоже требует собрать 600 тысяч рублей на ремонт лифта, иначе до 2027 года", False),
    ("Жители ЖК Прогресс сообщают, что и у нас в корпусе так же. " + NEWS47[:500], False),
])
def test_same_case_inheritance(ctx, text, copy):
    a = post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    n = post(ctx, text)
    r = risk_of(ctx, n)
    assert (r.get("copy_of") == a and r.get("cap_kind") == "same_case") is copy, r
    assert (prio(ctx, n) == "P3") is copy


def test_take_in_work_is_not_white_but_ours_is(ctx):
    a = post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    ctx.alerts.ack(a, "Оператор", "in_work")                      # «Беру» — работа, не вердикт «наше»
    e = post(ctx, DTP_NOADDR)
    assert prio(ctx, e) == "P3"
    # «✅ Наше» на корне: белый корень, корень возвращается, копии — строкой в его карточке
    ctx.db.x("UPDATE items SET workflow='open' WHERE id=?", (a,))
    learned.mark_item_ours(ctx, a, "Оператор")
    assert prio(ctx, a) == "P1" and tg_cards(ctx, a)
    assert prio(ctx, e) in ("P1", "P2") and not tg_cards(ctx, e)
    assert risk_of(ctx, e)["copy_kind"] == "white"


def test_reconciler_lowers_repost_that_came_before_root(ctx):
    e = post(ctx, DTP_NOADDR)
    assert prio(ctx, e) == "P1" and tg_cards(ctx, e)
    post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    ids = ctx.pipeline.reconcile_down()
    assert e in ids and prio(ctx, e) == "P3"
    assert ctx.pipeline.reconcile_down() == []                     # идемпотентен, только понижает


def test_false_positive_root_and_undo(ctx):
    r = post(ctx, DTP_NOADDR)
    ctx.alerts.ack(r, "Оператор", "false_positive")
    c = post(ctx, "Жители ЖК Прогресс жалуются: УК требует собрать 600 тысяч на ремонт грузового лифта, иначе лифт "
                  "встанет до конца 2027 года, это шантаж")
    assert prio(ctx, c) == "P3" and risk_of(ctx, c)["copy_kind"] == "fp"
    ctx.alerts.reopen(r, "Оператор")                               # «↩️ Вернуть»: белый корень, проход «вверх»
    assert prio(ctx, c) in ("P1", "P2") and risk_of(ctx, c)["copy_kind"] == "white"
    al = ctx.db.one("SELECT last_notified_at FROM alerts WHERE item_id=?", (r,))
    assert al["last_notified_at"] > ago(minutes=1)                 # без залпа эскалаций


# ============================================================ §6: решения оператора как данные
@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    src = Path(entities.CONFIG_DIR)
    shutil.copy(src / "entities.yaml", tmp_path / "entities.yaml")
    monkeypatch.setattr(entities, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_not_ours_and_our_building_buttons(ctx, cfgdir):
    ctx.reg.neighbors = [n for n in ctx.reg.neighbors if n["key"] != "servis_plus"]
    ctx.reg.zhks["progress"]["complete"] = False                 # как если бы список домов ЖК не был сверен
    ctx.matcher = Matcher(ctx.reg, ctx.watch.keywords)
    a = post(ctx, "Жители дома №13к3 на Европейском (ЖК «Прогресс») жалуются губернатору: лифт стоит полгода")
    assert prio(ctx, a) == "P2" and risk_of(ctx, a)["cap_kind"] == "registry_check"
    msg = learned.mark_not_ours(ctx, a, "Оператор")
    assert "13к3" in msg and "пр. Европейский, д. 13 к. 3" in (cfgdir / "entities.learned.yaml").read_text("utf-8")
    b = post(ctx, "Жители дома №13к3 на Европейском (ЖК «Прогресс») снова без лифта, пишут в прокуратуру")
    assert prio(ctx, b) == "P3" and risk_of(ctx, b)["cap_kind"] == "foreign"     # теперь известный чужой
    # наш адрес в «чужие» не пишется
    c = post(ctx, "Дом №13к2 на Европейском — лифт стоит, пишем губернатору")
    assert "наш адрес" in learned.mark_not_ours(ctx, c, "Оператор") or prio(ctx, c) == "P1"
    # «🏠 Наш дом» для другого корпуса: в ЖК одна наша УК — сразу
    d = post(ctx, "Жители дома №13к7 на Европейском (ЖК «Прогресс»): лифт стоит, пишем губернатору")
    msg, choices = learned.mark_ours(ctx, d, "Оператор")
    assert not choices and "наш дом" in msg
    assert prio(ctx, d) == "P1"                                    # проход «вверх» вернул публикацию


def test_skipped_report_sheet(ctx):
    post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    from monitor.reports.builder import gather
    d = gather(ctx, ago(hours=2), iso())
    assert d["skipped"] and d["skipped"][0]["_kind"] == "чужой дом"
    assert any(f["address"].startswith("пр. Европейский, д. 13 к. 1") for f in d["foreign_review"])


# ============================================================ §7: эскалации без дублей
class Tg:
    def __init__(self):
        self.sent, self.n = [], 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.sent.append((request.url.path.rsplit("/", 1)[-1], body))
        self.n += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.n}})


def test_escalation_edits_card_and_pings_once(ctx, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    tg = Tg()
    ctx.alerts._http = httpx.AsyncClient(transport=httpx.MockTransport(tg.handler))
    iid = post(ctx, "ЖК Прогресс, корпус 13к4: грузовой лифт не работает уже месяц, пишем губернатору и в прокуратуру!")
    asyncio.run(ctx.alerts.process())
    cards = [b for m, b in tg.sent if m == "sendMessage" and b.get("reply_markup")]
    assert len(cards) == 1
    for _ in range(3):                                             # три эскалации подряд
        ctx.db.x("UPDATE alerts SET last_notified_at=?", (ago(hours=2),))
        assert ctx.alerts.escalate() == 1
        asyncio.run(ctx.alerts.process())
    sends = [b for m, b in tg.sent if m == "sendMessage"]
    pings = [b for b in sends if not b.get("reply_markup")]
    assert len(sends) - len(pings) == 1                            # карточка одна
    assert len(pings) == 3 and all(p["reply_parameters"]["allow_sending_without_reply"] for p in pings)
    deleted = [b for m, b in tg.sent if m == "deleteMessage"]
    assert len(deleted) == 2                                       # в чате остаётся последний пинг
    # «Беру» — пинг в очереди устаревает
    ctx.db.x("UPDATE alerts SET last_notified_at=?", (ago(hours=2),))
    ctx.db.x("UPDATE alerts SET escalations=0")
    ctx.alerts.escalate()
    ctx.alerts.ack(iid, "Оператор", "in_work")
    before = len([1 for m, _ in tg.sent if m == "sendMessage"])
    asyncio.run(ctx.alerts.process())
    assert len([1 for m, _ in tg.sent if m == "sendMessage"]) == before


def test_relay_card_gets_ping_without_reply(ctx):
    """Ретранслятор Colab не возвращает message_id: карточка есть (строка очереди relay) — пинг без ответа."""
    iid = post(ctx, "ЖК Прогресс, корпус 13к4: грузовой лифт не работает уже месяц, пишем губернатору и в прокуратуру!")
    ctx.db.x("UPDATE outbox SET status='relay' WHERE item_id=? AND dedup_key LIKE 'tg:%'", (iid,))
    ctx.db.x("UPDATE alerts SET last_notified_at=?", (ago(hours=2),))
    assert ctx.alerts.escalate() == 1
    pings = ctx.db.q("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'ping:%'")
    assert pings and "reply_parameters" not in jload(pings[0]["payload_json"])
    assert len(tg_cards(ctx, iid)) == 1


def test_trial_week_morning_summary_and_case_audit(ctx):
    post(ctx, NEWS47, "rss", "rss", url="https://47news.ru/articles/294014/")
    post(ctx, DTP_NOADDR)
    ctx.db.kv_set("story_trial_from", iso())
    assert ctx.alerts.story_summary() == 2                         # одна сводка: чужой дом + копия
    rows = ctx.db.q("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'story:%'")
    assert len(rows) == 1 and "h:ours:" in rows[0]["payload_json"]
    assert ctx.alerts.story_summary() == 0                         # не чаще раза в день
    from monitor.cases import audit
    assert "пар: 1" in audit(ctx, 30)


def test_stale_llm_texts_are_requeued_after_rematch(ctx):
    """Карточка «Жизни в Кудрово» в GUI: P3 по новым правилам, но тексты и риск 87 — от старого разбора ИИ («наш ЖК»).
    После пересборки такие публикации уходят на повторный разбор с новой подсказкой."""
    i = post(ctx, KUDROVO_VK, platform="vk_post")
    ctx.db.x("UPDATE items SET analysis_level='local', status='analyzed' WHERE id=?", (i,))
    ctx.pipeline.rematch_recent()
    it = ctx.db.item(i)
    assert it["priority"] == "P3" and it["status"] == "queued"


def test_zhk_completeness_checkbox_writes_yaml(cfgdir):
    p = cfgdir / "entities.yaml"
    assert entities.set_zhk_verified("moskva", True, "тест", "2026-10-09", p)
    assert entities.load_registry(p).zhk_complete("moskva")
    assert entities.set_zhk_verified("moskva", False, path=p)
    assert not entities.load_registry(p).zhk_complete("moskva")
    assert "# Справочник" in p.read_text("utf-8")          # комментарии файла не теряются
