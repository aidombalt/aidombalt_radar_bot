"""Кандидаты в источники: только с доказательствами; в боте — одно сообщение со ссылками и отметками."""
import asyncio

from monitor import candidates as cands
from monitor.util import iso, jdump


def test_evidence_rules(ctx):
    ev = lambda t: cands.evidence(ctx, t, "https://vk.ru/wall-1_1", iso())  # noqa: E731
    assert ev("УК Этажи опять не вывезли мусор у Ручьевского 6")["obj"]
    assert ev("ЖК Виктория в Мурино: лифт стоит неделю")["obj"] == "ЖК Виктория"     # ЖК + наш район
    assert ev("В Выборге сдали ЖК Юбилей, новоселы довольны") is None                 # тёзка без нашего района
    assert ev("Работа в Кудрово, подработка курьером") is None


class FakeApi:
    def __init__(self, resp):
        self.resp = resp

    def calls_today(self, m):
        return 0

    async def call(self, method, params, user=False):
        return self.resp


def _search(ctx, posts, groups):
    from monitor.collectors.vk import VKSearchCollector

    col = VKSearchCollector(ctx, FakeApi({"items": posts, "groups": groups, "profiles": []}))
    return asyncio.run(col.fetch({"handle": "УК Этажи", "key": "vk_search:УК Этажи"}))


def test_search_proposes_only_with_evidence(ctx):
    ctx.db.upsert_source({"key": "vk_wall:kudrovolife", "kind": "vk_wall", "handle": "kudrovolife", "title": "Жизнь в Кудрово",
                          "meta": {"owner_id": -51766355}})
    now = 1790000000
    posts = [
        {"owner_id": -203351035, "id": 7, "from_id": -203351035, "date": now, "text": "УК Этажи, Ручьевский 6 — третий день мусор"},
        {"owner_id": -37888583, "id": 8, "from_id": -37888583, "date": now, "text": "ЖК Юбилей в Выборге: новости стройки"},
        {"owner_id": -51766355, "id": 9, "from_id": -51766355, "date": now, "text": "УК Этажи в Кудрово? Ручьевский 6"},
        {"owner_id": -171774082, "id": 10, "from_id": -171774082, "date": now, "text": "Работа подработка Кудрово, курьеры"},
    ]
    groups = [{"id": 203351035, "name": "Подслушано Мурино|Цветной Город|Девяткино|Бугры", "members_count": 8262,
               "screen_name": "podslushano_murino"},
              {"id": 37888583, "name": "Интересные события в Выборге", "members_count": 94582},
              {"id": 51766355, "name": "Жизнь в Кудрово", "members_count": 83016},
              {"id": 171774082, "name": "Работа подработка Кудрово", "members_count": 4397}]
    _search(ctx, posts, groups)
    got = {c["key"]: c for c in cands.listing(ctx)}
    assert list(got) == ["vk_wall:-203351035"]                   # Выборг, дубль «Жизни в Кудрово» и «Работа» — нет
    c = got["vk_wall:-203351035"]
    assert c["url"] == "https://vk.com/podslushano_murino" and c["n"] == 1 and "Ручьевский" in c["ev"][0]["snip"]
    posts[0] = {**posts[0], "id": 11}
    _search(ctx, posts, groups)                                   # второе упоминание — счётчик, не новый кандидат
    assert cands.listing(ctx)[0]["n"] == 2


def test_review_old_candidates(ctx):
    for oid, title in ((-1001, "Интересные события в Выборге"), (-1002, "Мурино онлайн")):
        ctx.db.upsert_source({"key": f"vk_wall:{oid}", "kind": "vk_wall", "handle": str(oid), "title": title,
                              "candidate": 1, "members": 5000, "note": "найдено поиском: здесь упоминали наши объекты"})
    ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at) "
             "VALUES('vk_post','vk:-1002_5','vk_search:x','https://vk.ru/wall-1002_5',"
             "'Цвета Радуги, Шувалова 44 — опять нет горячей воды',?,?)", (iso(), iso()))
    st = cands.review(ctx)
    assert st == {"kept": 1, "hidden": 1, "dup": 0}
    assert [c["title"] for c in cands.listing(ctx)] == ["Мурино онлайн"]
    # скрытый вернётся сам, если появится настоящее упоминание
    ev = cands.evidence(ctx, "УК Приморский дом, Шувалова 44 — протечка", "https://vk.ru/wall-1001_9", iso())
    assert cands.propose(ctx, -1001, {}, ev) and len(cands.listing(ctx)) == 2


def test_bot_candidates_one_message(ctx):
    from aiogram.methods import EditMessageText, SendMessage
    from aiogram.types import CallbackQuery, Message, Update, User

    from monitor.alerts.bot import build_bot
    from monitor.service import Service
    from tests.test_bot import CHAT, NOW, MockSession, msg

    for i in range(8):
        ctx.db.upsert_source({"key": f"vk_wall:-{500 + i}", "kind": "vk_wall", "handle": f"-{500 + i}", "title": f"Паблик {i}",
                              "candidate": 1, "members": 1000 * (i + 1), "enabled": 0,
                              "meta_json": jdump({"owner_id": -(500 + i), "n": 1, "ev": [
                                  {"url": f"https://vk.ru/wall-{500 + i}_1", "snip": "Шувалова 44 без воды",
                                   "obj": "ЖК Цвета Радуги", "strong": "address", "at": iso()}]})})
    sess = MockSession()
    bot, dp = build_bot(ctx, Service(ctx), session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")

    def press(data, n):
        return dp.feed_update(bot, Update(update_id=n, callback_query=CallbackQuery(
            id=f"c{n}", from_user=User(id=111, is_bot=False, first_name="Оператор"), chat_instance="x", data=data,
            message=Message(message_id=7, date=NOW, chat=CHAT, text="list"))))

    async def go():
        await dp.feed_update(bot, msg("/cand"))
        first = [m for m in sess.sent if isinstance(m, SendMessage)]
        assert len(first) == 1                                            # одно сообщение, а не восемь
        text, kb = first[0].text, first[0].reply_markup.inline_keyboard
        assert "1–6 из 8" in text and 'href="https://vk.com/club507"' in text and ">пост</a>" in text
        tok = kb[0][0].callback_data.split(":")[1]
        await press(f"k:{tok}:t1", 2)
        await press(f"k:{tok}:t2", 3)
        edit = [m for m in sess.sent if isinstance(m, EditMessageText)][-1]
        assert edit.reply_markup.inline_keyboard[0][0].text == "☑️ 1"
        await press(f"k:{tok}:add", 4)
        await press(f"k:{tok}:nx", 5)
        last = [m for m in sess.sent if isinstance(m, EditMessageText)][-1]
        assert "7–8 из 8" in last.text
    asyncio.run(go())
    added = {r["key"] for r in ctx.db.q("SELECT key FROM sources WHERE candidate=0 AND enabled=1 AND key LIKE 'vk_wall:-5%'")}
    assert added == {"vk_wall:-507", "vk_wall:-506"}                    # отсортированы по аудитории: 1 и 2 — самые большие
