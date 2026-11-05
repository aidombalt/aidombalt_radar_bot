"""30.09, утро: добавили депутатов и активистов на контроль — посыпались письма. Разбираем тот же набор публикаций.

Было 8 писем за 3 минуты: посты Хмелёвой месячной давности, каждый P1 по два раза («[Telegram недоступен]»),
три P1 про одну и ту же воду на Екатерининской 11. Должно быть: история — в архив, свежие находки после подключения —
одной сводкой, будничные жалобы депутатов — P2, повторы по одному инциденту — в карточку."""
from datetime import timedelta

from monitor.context import Watch
from monitor.util import iso, jdump, now_utc

PARENT = "Жители ЖК Лампо в Мурино снова жалуются: вода, подвалы, УК «Возрождение Северо-Запада» не реагирует"
TOMA_BASEMENT = "Подвал под детским садом частично затоплен уже на протяжении нескольких месяцев. Решение проблемы ук игнорируется. Фото от 29.09.26 [фото]"
TOMA_WATER = "Четверо суток в доме Екатерининская 11, нет воды (ни ГВС, ни ХВС)‼️ Жителям не оказывается никакая поддержка от ук"
IVANOV = ("Екатерининская 11. Проблемы с водой уже больше 3 месяцев. Грязная горячая вода. Температура ГВС низкая, приходится "
          "долго прогонять пока пойдет теплая. ХВС тоже не самая чистая. УК молчит... Когда это закончится!? "
          "[club178555683|Александр Дрозденко] прошу Вас обратить внимание на сложившуюся затянувшуюся коммунальную "
          "катастрофу в нашем ЖК.")
KHMELEVA = ("#ЛампоVer2. Дома сданы в 2019 и 2022 году, выглядят, как после катаклизма. Швы не заделаны, фасадные работы не "
            "завершены, Ливневка не работает, течет крыша, текут швы, в подъездах не сделаны потолки. ЖК Лампо, Мурино.")


def llm(topics, **kw):
    """Ответ модели, как в письмах 30.09: всё P1, «инфлюенсер», негатив."""
    return {"relevant": True, "relation": "our", "sentiment": -2, "tone": "жалоба", "topics": topics, "addressees": [],
            "author_type": "инфлюенсер", "call_to_action": False, "hazard": False, "virality": "средняя", "priority": "P1",
            "summary": "жалоба", "why": "инфлюенсер", "action": "Проверить факт на объекте и ответить от лица УК",
            "reply_draft": "", "incident_hint": "Лампо", "entities": [], **kw}


def setup(ctx):
    ctx.watch = Watch({"people": [
        {"name": "Тамара Родимцева", "aliases": ["Тома Родимцева"], "vk_id": "toma_rodimceva"},
        {"name": "Анна Хмелёва", "aliases": ["Хмелёва Анна"], "vk_id": "hmelevaaa"},
        {"name": "Максим Смирнов", "aliases": ["Смирнов Максим"], "vk_id": "maxim10921"}]})
    src = {"key": "vk_wall:maxim10921", "kind": "vk_wall", "handle": "maxim10921", "title": "Максим Смирнов",
           "category": "influencer", "region": "LO"}
    ctx.db.upsert_source(src)
    return src


def raw(eid, text, author, followers, hours_ago, parent=None, platform="vk_comment"):
    return {"platform": platform, "external_id": eid, "url": f"https://vk.com/wall1_{eid}", "author_id": f"vk:{abs(hash(author)) % 10**6}",
            "author_name": author, "author_followers": followers, "text": text, "parent_text": parent,
            "published_at": iso(now_utc() - timedelta(hours=hours_ago))}


def analyze_all(ctx, answers):
    for eid, a in answers.items():
        it = ctx.db.one("SELECT id, candidate FROM items WHERE external_id=?", (eid,))
        if it and it["candidate"]:
            ctx.pipeline.finalize(it["id"], a, "local")


def sent(ctx, like):
    return ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE ?", (like,), 0)


def test_history_of_watched_people_is_archived(ctx):
    src = setup(ctx)
    ctx.pipeline.ingest([raw("kh1", KHMELEVA, "Анна Хмелёва", 2036, 24 * 35, platform="vk_post"),
                         raw("kh2", KHMELEVA + " Отчёт о встречах.", "Анна Хмелёва", 2036, 24 * 23, platform="vk_post")], src)
    assert ctx.db.scalar("SELECT COUNT(*) FROM items WHERE candidate=1", (), 0) == 0   # месячной давности — в архив


def test_morning_of_30_09_is_one_digest(ctx):
    """Находки 13–15-часовой давности после подключения источника — одной сводкой, а не восемью письмами."""
    src = setup(ctx)
    ctx.db.x("UPDATE users SET email='op@dombalt.ru' WHERE tg_id='111'")
    ctx.pipeline.ingest([raw("t1", TOMA_BASEMENT, "Тома Родимцева", 6151, 15.3, PARENT),
                         raw("t2", TOMA_WATER, "Тома Родимцева", 6151, 15.2, PARENT),
                         raw("i1", IVANOV, "Максим Иванов", 172, 12.8, PARENT)], src)
    analyze_all(ctx, {"t1": llm(["подвал_канализация"]), "t2": llm(["холодная_вода", "горячая_вода"]),
                      "i1": llm(["горячая_вода", "холодная_вода"], addressees=["губернатор ЛО"])})
    assert sent(ctx, "tg:%") == 0 and sent(ctx, "mail:%") == 0                      # поштучно — ничего
    assert ctx.alerts.flush_digest(settle_min=0, max_wait_min=0) == 3
    assert sent(ctx, "digest:%") == 1                                                 # одно сообщение оператору
    mails = ctx.alerts.recipients("p1", "mail")
    assert len(mails) == 2 and sent(ctx, "digestmail:%") == len(mails)              # по одному письму на человека (есть P1)
    text = ctx.db.scalar("SELECT payload_json FROM outbox WHERE dedup_key LIKE 'digest:%'")
    assert "Найдено с опозданием: 3" in text and "a:card:" in text
    assert "объект не определён" not in text                                          # объект взят из поста
    # эскалаций по сводке не будет
    assert ctx.db.scalar("SELECT MIN(escalations) FROM alerts", (), 0) >= ctx.settings.alerts.max_escalations
    prios = {r["external_id"]: r["priority"] for r in ctx.db.q("SELECT external_id, priority FROM items")}
    assert prios == {"t1": "P2", "t2": "P1", "i1": "P1"}
    # хроника под детсадом — P2: охват автора без второго ключа; вода четверо суток — P1; губернатор — P1


def test_same_publications_fresh(ctx):
    """Те же публикации, но свежие: отдельные алерты, но без удвоения писем и без будничного P1."""
    src = setup(ctx)
    ctx.pipeline.ingest([raw("t1", TOMA_BASEMENT, "Тома Родимцева", 6151, 0.6, PARENT),
                         raw("t2", TOMA_WATER, "Тома Родимцева", 6151, 0.5, PARENT),
                         raw("t3", "На Екатерининской 11 до сих пор нет воды, ни горячей, ни холодной — пятые сутки", "Тома Родимцева",
                             6151, 0.3, PARENT)], src)
    analyze_all(ctx, {"t1": llm(["подвал_канализация"]), "t2": llm(["холодная_вода", "горячая_вода"]),
                      "t3": llm(["холодная_вода"])})
    alerted = {r["external_id"] for r in ctx.db.q("SELECT i.external_id FROM alerts a JOIN items i ON i.id=a.item_id")}
    assert alerted == {"t1", "t2"}                    # третий — та же вода на том же доме: дописан в карточку, без сообщения
    assert sent(ctx, "mail:%") == 1                   # письмо только по P1 (вода), и одно
    ctx.pipeline.ingest([raw("i1", IVANOV, "Максим Иванов", 172, 0.1, PARENT)], src)
    analyze_all(ctx, {"i1": llm(["горячая_вода", "холодная_вода"], addressees=["губернатор ЛО"])})
    assert ctx.db.one("SELECT 1 FROM alerts a JOIN items i ON i.id=a.item_id WHERE i.external_id='i1'")   # дошло до губернатора


def test_fallback_mail_only_to_the_person_who_missed_telegram(ctx):
    import asyncio

    src = setup(ctx)
    ctx.db.x("UPDATE users SET email='op@dombalt.ru' WHERE tg_id='111'")
    ctx.db.x("INSERT INTO users(tg_id, name, role, level, active, email) VALUES('222','Никита','manager',2,1,'nik@dombalt.ru')")
    ctx.pipeline.ingest([raw("t2", TOMA_WATER, "Тома Родимцева", 6151, 0.5, PARENT)], src)
    analyze_all(ctx, {"t2": llm(["холодная_вода"])})
    mails_before = sent(ctx, "mail:%")

    async def fake_send(payload):
        if str(payload.get("chat_id")) == "222":
            raise RuntimeError("Telegram 400: Bad Request: chat not found")
        return {"message_id": 1}
    ctx.alerts.send_tg = fake_send

    async def no_mail(*a, **k):
        return None
    import monitor.alerts.outbox as ob
    ob.send_email = no_mail
    asyncio.run(ctx.alerts.process())
    fb = [r["recipient"] for r in ctx.db.q("SELECT recipient FROM outbox WHERE dedup_key LIKE 'fallback:%'")]
    assert fb in ([], ["nik@dombalt.ru"])             # только тому, до кого не дошло (если он не получает письмо и так)
    assert "op@dombalt.ru" not in fb and sent(ctx, "mail:%") == mails_before
    assert sent(ctx, "adm:nostart:222:%:111") == 1     # админу — одно сообщение: «Никита не нажал Старт»
