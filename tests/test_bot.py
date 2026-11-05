"""Бот без сети: подменяем HTTP-сессию aiogram и «кормим» диспетчер апдейтами."""
import asyncio
from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from monitor.alerts.bot import build_bot
from monitor.service import Service
from monitor.util import iso

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
CHAT = Chat(id=111, type="private")


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        return Message(message_id=len(self.sent), date=NOW, chat=CHAT, text=getattr(method, "text", "")).as_(bot)

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""


def msg(text, uid=111):
    return Update(update_id=1, message=Message(message_id=1, date=NOW, chat=Chat(id=uid, type="private"),
                                               from_user=User(id=uid, is_bot=False, first_name="Оператор"), text=text))


def test_bot_commands_and_buttons(ctx):
    svc = Service(ctx)
    sess = MockSession()
    bot, dp = build_bot(ctx, svc, session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")

    async def go():
        await dp.feed_update(bot, msg("/start", uid=999))                  # чужой — видит свой id, без меню
        await dp.feed_update(bot, msg("/status", uid=999))                 # чужой — нет доступа
        await dp.feed_update(bot, msg("/status"))
        await dp.feed_update(bot, msg("/replay Александр Юрьевич, ЖК Виктория в Мурино — лифт стоит неделю, помогите!"))
        await dp.feed_update(bot, msg("/src_add vk murino_typical warm"))
        await dp.feed_update(bot, msg("/set schedule.hot_minutes 3"))
        # публикация + кнопка «Беру в работу»
        ctx.pipeline.ingest([{"platform": "vk_comment", "external_id": "vk:1_1_1", "author_id": "vk:5",
                              "text": "УК Этажи, Ручьевский 6 — третий день нет воды, сколько можно! Александр Юрьевич!",
                              "published_at": iso()}], {"key": "vk_wall:drozdenko_a", "category": "governor", "region": "LO"})
        iid = ctx.db.one("SELECT id FROM items WHERE external_id='vk:1_1_1'")["id"]
        ctx.pipeline.rules_analyze(iid)
        cb = CallbackQuery(id="c1", from_user=User(id=111, is_bot=False, first_name="Оператор"), chat_instance="x",
                           data=f"a:ack:{iid}", message=Message(message_id=5, date=NOW, chat=CHAT, text="alert"))
        await dp.feed_update(bot, Update(update_id=2, callback_query=cb))
        await dp.feed_update(bot, msg("В чате дома на Шувалова 44 пишут, что лифт стоит третий день"))
        return iid

    iid = asyncio.run(go())
    texts = [m.text for m in sess.sent if isinstance(m, SendMessage)]
    assert "999" in texts[0] and "Доступа пока нет" in texts[0]
    assert "Нет доступа" in texts[1]
    assert any("Радар — состояние" in t for t in texts)
    assert any("Ретро-проверка" in t and "P1" in t for t in texts), texts
    assert ctx.db.one("SELECT 1 FROM sources WHERE key='vk_wall:murino_typical'")
    assert ctx.settings.schedule.hot_minutes == 3
    assert ctx.db.one("SELECT acked_by FROM alerts WHERE item_id=?", (iid,))["acked_by"] == "Оператор"
    assert ctx.db.item(iid)["workflow"] == "in_work"
    fw = ctx.db.one("SELECT * FROM items WHERE platform='manual'")
    assert fw and fw["candidate"] == 1 and fw["status"] == "queued" and "Шувалова" in fw["text"]
    assert any("Принято в анализ" in t for t in texts)


def test_false_positive_collapses_card_and_undo_restores(ctx):
    from aiogram.methods import EditMessageText

    svc = Service(ctx)
    sess = MockSession()
    bot, dp = build_bot(ctx, svc, session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")
    ctx.pipeline.ingest([{"platform": "vk_comment", "external_id": "vk:1_1_9", "author_id": "vk:5",
                          "text": "ЖК Виктория, Авиаторов Балтики 9к1 — лифт стоит вторую неделю! Александр Юрьевич!",
                          "published_at": iso()}], {"key": "vk_wall:drozdenko_a", "category": "governor", "region": "LO"})
    iid = ctx.db.one("SELECT id FROM items WHERE external_id='vk:1_1_9'")["id"]
    ctx.pipeline.rules_analyze(iid)
    prio = ctx.db.item(iid)["priority"]

    def press(data, n):
        return Update(update_id=n, callback_query=CallbackQuery(
            id=f"c{n}", from_user=User(id=111, is_bot=False, first_name="Оператор"), chat_instance="x", data=data,
            message=Message(message_id=5, date=NOW, chat=CHAT, text="alert")))

    async def go():
        await dp.feed_update(bot, press(f"a:fp:{iid}", 10))
        folded = [m for m in sess.sent if isinstance(m, EditMessageText)][-1].text
        assert ctx.db.item(iid)["workflow"] == "false_positive" and folded.startswith("❌")
        assert not any(isinstance(m, SendMessage) for m in sess.sent)     # никаких новых сообщений в чат
        await dp.feed_update(bot, press(f"a:undo:{iid}", 11))
        restored = [m for m in sess.sent if isinstance(m, EditMessageText)][-1].text
        return restored

    restored = asyncio.run(go())
    it = ctx.db.item(iid)
    assert it["workflow"] == "open" and it["priority"] == prio and "Суть" in restored or "лифт" in restored
