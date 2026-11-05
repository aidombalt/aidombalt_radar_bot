"""Добавление по ссылке: распознавание, источник, человек на контроль, бот."""
import asyncio

import yaml

from monitor import linkadd as la


def test_parse_links():
    assert la.parse("https://t.me/s/kudrovo_life") == ("tg", "kudrovo_life")
    assert la.parse("t.me/kudrovo_life/1234") == ("tg", "kudrovo_life")          # ссылка на пост → канал
    assert la.parse("@murino_live") == ("tg", "murino_live")
    assert la.parse("https://vk.com/wall-123_45") == ("vk", "club123")             # пост → стена сообщества
    assert la.parse("vk.ru/toma_rodimceva?w=wall1_2") == ("vk", "toma_rodimceva")
    assert la.parse("https://m.vk.com/id777") == ("vk", "id777")
    assert la.parse("https://fontanka.ru/") == ("web", "https://fontanka.ru/")
    assert isinstance(la.parse("t.me/+AbCdEf"), str)                               # закрытый чат
    assert isinstance(la.parse("vk.com/feed"), str)
    assert isinstance(la.parse("просто текст про Мурино"), str)


def test_guesses_and_errors():
    assert la.guess_region("Горячие новости Мурино") == "LO" and la.guess_region("Приморский район СПб") == "SPB"
    assert la.guess_category("Госжилнадзор Ленобласти", False, "vk") == "official"
    assert la.guess_category("Тамара Р.", True, "vk") == "influencer"
    assert la.default_aliases("Тамара Родимцева") == ["Родимцева Тамара", "Т. Родимцева", "Родимцева"]
    assert la.human_error("HTTPStatusError: Client error '403 Forbidden' for url 'https://t.me/s/x'") == "площадка не пускает (403)"


def test_add_source_and_person(ctx, tmp_path, monkeypatch):
    f = la.Found("vk", "murino_new", kind="vk_wall", title="Новости Мурино", members=12000, uid=-555, region="LO",
                 comments=True)
    key = la.add_source(ctx, f, tier="hot", category="public", by="тест")
    s = ctx.db.one("SELECT * FROM sources WHERE key=?", (key,))
    assert s["enabled"] == 1 and s["tier"] == "hot" and s["members"] == 12000 and '"owner_id": -555' in s["meta_json"]
    assert la.existing_key(ctx, "vk_wall", "MURINO_NEW") == key
    assert la.similar_title(ctx, "новости мурино") == "Новости Мурино"

    from monitor.context import Watch
    wl = tmp_path / "watchlist.yaml"
    wl.write_text("people: []\n", "utf-8")
    monkeypatch.setattr(la, "CONFIG_DIR", tmp_path)

    def save(d):
        wl.write_text(yaml.safe_dump(d, allow_unicode=True), "utf-8")
        ctx.watch = Watch(d)
    monkeypatch.setattr(ctx, "save_watchlist", save)
    monkeypatch.setattr(ctx, "sync_sources_from_yaml", lambda *a: 0)

    vk = la.Found("vk", "ivan_petrov", kind="vk_wall", title="Иван Петров", person=True, uid=4242, members=900)
    la.add_person(ctx, [vk], name="Иван Петров", aliases=la.default_aliases("Иван Петров"), note="активист")
    tg = la.Found("tg", "ivanp", kind="tg_person", title="Иван", person=True)
    la.add_person(ctx, [tg], name="иван петров")                                     # вторая ссылка — тому же человеку
    people = yaml.safe_load(wl.read_text("utf-8"))["people"]
    assert len(people) == 1 and people[0]["vk_uid"] == 4242 and people[0]["tg"] == "ivanp"
    assert people[0]["tg_channel"] is False and people[0]["note"] == "активист"
    w = ctx.watch
    assert w.author_watched({"author_id": "vk:4242"}) == "Иван Петров"               # его комментарий в любом паблике
    assert w.author_watched({"author_id": "tg:other", "text": "пишет @ivanp про наш двор"}) == "Иван Петров"
    assert la.watched_by_link(ctx, vk) == "Иван Петров"
    assert la.remove_person(ctx, "Иван Петров") and not ctx.watch.people


def test_bot_link_flow(ctx, monkeypatch):
    from aiogram.methods import EditMessageText
    from aiogram.types import CallbackQuery, Message, Update, User

    from monitor.alerts.bot import build_bot
    from monitor.service import Service
    from tests.test_bot import CHAT, NOW, MockSession, msg

    async def fake_preview(ctx_, service, link):
        return la.Found("tg", "kudrovo_life", link=link, kind="tg_channel", title="Жизнь в Кудрово", members=28100,
                        region="LO", category="public", tier="hot")
    monkeypatch.setattr(la, "preview", fake_preview)
    svc = Service(ctx)
    svc.collectors = {}                       # первый опрос в тесте не нужен
    sess = MockSession()
    bot, dp = build_bot(ctx, svc, session=sess, token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz")

    async def go():
        await dp.feed_update(bot, msg("https://t.me/kudrovo_life"))
        card = [m for m in sess.sent if isinstance(m, EditMessageText)][-1]
        assert "Жизнь в Кудрово" in card.text and "Паблик" in card.text
        data = card.reply_markup.inline_keyboard[0][0].callback_data
        assert data.startswith("l:hot:")
        await dp.feed_update(bot, Update(update_id=5, callback_query=CallbackQuery(
            id="c9", from_user=User(id=111, is_bot=False, first_name="Оператор"), chat_instance="x", data=data,
            message=Message(message_id=7, date=NOW, chat=CHAT, text="card"))))
    asyncio.run(go())
    s = ctx.db.one("SELECT * FROM sources WHERE key='tg_channel:kudrovo_life'")
    assert s and s["tier"] == "hot" and s["enabled"] == 1
    assert not ctx.db.one("SELECT 1 FROM items WHERE platform='manual'")          # ссылка не ушла «в анализ»


def test_user_config_template(tmp_path, monkeypatch):
    from monitor import settings
    (tmp_path / "watchlist.example.yaml").write_text("people: []\n", "utf-8")
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path)
    settings.ensure_user_configs()
    assert (tmp_path / "watchlist.yaml").read_text("utf-8") == "people: []\n"
    (tmp_path / "watchlist.yaml").write_text("people: [{name: X}]\n", "utf-8")
    settings.ensure_user_configs()                                  # своя копия не перезаписывается
    assert "X" in (tmp_path / "watchlist.yaml").read_text("utf-8")
