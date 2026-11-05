"""Сквозные проверки без сети: ВК-коллектор на фикстурах API → конвейер → риск → алерт → отчёт."""
import asyncio
import time

import docx
import openpyxl

from monitor.brain.core import analyze
from monitor.collectors.vk import VKCollector
from monitor.reports import builder
from monitor.util import iso, jload

NOW = int(time.time()) - 3600  # час назад: окно комментариев (72 ч) считается от текущего времени


class FakeVK:
    """Имитирует ответы VK API (форматы v5.199)."""
    ready = True

    def __init__(self):
        self.calls = []

    async def call(self, method, params, user=False):
        self.calls.append(method)
        if method == "utils.resolveScreenName":
            return {"type": "user", "object_id": 91919}
        if method == "users.get":
            return [{"id": 91919, "first_name": "Александр", "last_name": "Дрозденко", "followers_count": 150000}]
        if method == "wall.get":
            return {"items": [{"id": 5001, "owner_id": 91919, "from_id": 91919, "date": NOW,
                               "text": "Прямая линия: задавайте ваши вопросы о благоустройстве Всеволожского района",
                               "comments": {"count": 3}, "likes": {"count": 900}, "reposts": {"count": 12},
                               "views": {"count": 40000}}],
                    "profiles": [{"id": 91919, "first_name": "Александр", "last_name": "Дрозденко", "followers_count": 150000}],
                    "groups": []}
        if method == "wall.getComments":
            return {"items": [
                {"id": 77, "from_id": 555, "date": NOW + 60,
                 "text": "Александр Юрьевич! ЖК Виктория, Мурино, Авиаторов Балтики 9к1 — лифт стоит вторую неделю, "
                         "УК Балтийский Дом не отвечает. Сколько можно?! Пишите все губернатору!",
                 "likes": {"count": 41}, "thread": {"count": 1, "items": [
                     {"id": 78, "from_id": 556, "date": NOW + 120, "text": "У нас на Екатерининской 22 то же самое",
                      "likes": {"count": 5}}]}},
                {"id": 76, "from_id": 557, "date": NOW + 30, "text": "Спасибо за дороги!", "likes": {"count": 2}}],
                "profiles": [{"id": 555, "first_name": "Тамара", "last_name": "Р.", "followers_count": 12400},
                             {"id": 556, "first_name": "Иван", "last_name": "П.", "followers_count": 10},
                             {"id": 557, "first_name": "Ольга", "last_name": "С.", "followers_count": 3}]}
        raise AssertionError(method)


def run(coro):
    return asyncio.run(coro)


def test_vk_comments_pipeline_and_alert(ctx):
    vk = VKCollector(ctx)
    vk.api = FakeVK()
    src = dict(ctx.db.one("SELECT * FROM sources WHERE key='vk_wall:drozdenko_a'"))
    src["meta"] = {}
    stats = run(vk.run([src]))
    assert stats["ok"] == 1 and stats["new"] == 4, stats
    # комментарии связаны с постом, пост опознан как «прямая линия»
    c = ctx.db.one("SELECT * FROM items WHERE external_id='vk:91919_5001_77'")
    assert c["parent_external_id"] == "vk:91919_5001" and "Прямая линия" in c["parent_text"]
    assert c["author_followers"] == 12400 and c["candidate"] == 1 and c["urgency"] == 1
    assert ctx.db.cursor_get("vk:hot:91919_5001")
    # повторный опрос ничего не дублирует (курсоры по комментариям)
    stats2 = run(vk.run([src]))
    assert stats2["new"] == 0
    # разбор правилами (ИИ недоступен) → P1 + алерт в очереди доставки
    res = ctx.pipeline.rules_analyze(c["id"])
    assert res["priority"] == "P1", res
    it = ctx.db.item(c["id"])
    tags = jload(it["tags"])
    assert "ЖК:Виктория" in tags and "без_ИИ" in tags and "призыв" in tags
    out = ctx.db.q("SELECT channel, recipient FROM outbox WHERE item_id=?", (c["id"],))
    assert {r["channel"] for r in out} == {"telegram", "email"}
    # «спасибо за дороги» — не кандидат
    assert ctx.db.one("SELECT candidate FROM items WHERE external_id='vk:91919_5001_76'")["candidate"] == 0


class FakeLLM:
    """LLM-заглушка: первый ход просит инструмент, второй — выдаёт анализ."""
    vision, name = False, "fake"

    def __init__(self):
        self.n = 0

    async def chat_json(self, messages, schema, **kw):
        self.n += 1
        if "action" in schema.get("properties", {}) and self.n == 1:
            return {"action": "tools", "calls": [{"name": "author_history", "query": "автор"}]}
        assert "РЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ" in messages[-1]["content"]
        return {"action": "final", "analysis": {
            "relevant": True, "relation": "our", "sentiment": -2, "tone": "жалоба", "topics": ["лифт"],
            "priority": "P1", "summary": "Лифт в ЖК Виктория не работает 2 недели, жалоба губернатору ЛО",
            "why": "адресовано губернатору, призыв", "action": "Ответить от УК в течение 30 минут",
            "reply_draft": "Здравствуйте! Бригада направлена…", "addressees": ["губернатор ЛО"],
            "author_type": "инфлюенсер", "call_to_action": True}}


def test_agent_tool_round_and_colab_finalize(ctx):
    ctx.pipeline.ingest([{"platform": "vk_comment", "external_id": "vk:1_2_3", "url": "https://vk.com/wall1_2?reply=3",
                          "author_id": "vk:555", "author_name": "Тамара Р.", "author_followers": 12400,
                          "text": "ЖК Виктория Мурино, лифт стоит две недели, Александр Юрьевич помогите!",
                          "published_at": iso()}], {"key": "vk_wall:drozdenko_a", "category": "governor", "region": "LO"})
    item = ctx.db.one("SELECT * FROM items WHERE external_id='vk:1_2_3'")
    pkg = ctx.memory.package(dict(item), ctx.reg.catalog_text(), {"title": "Дрозденко", "category": "governor"})
    assert "Мурино" in pkg["catalog"] and pkg["item"]["author_name"] == "Тамара Р."   # инфлюенсер — имя не скрыто
    a = run(analyze(pkg, FakeLLM(), ctx.memory.tool, max_rounds=2))
    assert a["priority"] == "P1" and a["_meta"]["tools"] == ["author_history(автор)"]
    ctx.pipeline.finalize(item["id"], a, "colab")
    it = ctx.db.item(item["id"])
    assert it["priority"] == "P1" and it["analysis_level"] == "colab" and it["incident_id"]


def test_pii_masking(ctx):
    ctx.pipeline.ingest([{"platform": "vk_comment", "external_id": "vk:9_9_9", "author_id": "vk:1", "author_name": "Иван Иванов",
                          "author_followers": 12, "text": "УК Этажи, кв. 45, звоните +7 911 123-45-67, лифт!",
                          "published_at": iso()}], {"key": "x"})
    item = ctx.db.one("SELECT * FROM items WHERE external_id='vk:9_9_9'")
    pkg = ctx.memory.package(dict(item), "", {})
    assert "911" not in pkg["item"]["text"] and "[№]" in pkg["item"]["text"]
    assert pkg["item"]["author_name"] == "житель (имя скрыто)"


def test_reports_with_and_without_data(ctx, tmp_path):
    from datetime import datetime, timedelta, timezone
    far = datetime.now(timezone(timedelta(hours=3))) + timedelta(hours=6)     # не «утренний»: иначе тест зависит от часов
    ctx.settings.schedule.reports = [f"{far:%H:%M}"]
    r0 = run(builder.build(ctx, hours=1, send=True))
    wb = openpyxl.load_workbook(r0["xlsx"])
    assert wb.sheetnames == ["Сводка", "Инциденты", "Сюжеты", "Упоминания", "Лента по часам", "Динамика постов", "Не присланы", "Площадки"]
    assert "нет" in wb["Упоминания"]["A2"].value.lower() and r0["level"] == "green"
    assert wb["Сводка"]["A4"].value.startswith("🟢")
    # дневной отчёт руководству (pr@…) по умолчанию не уходит, оператору в Telegram — уходит; недельный — всем
    assert ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE 'report:%'") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE 'reptg:%'") == 1
    run(builder.build(ctx, hours=1, send=True, kind="weekly"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM outbox WHERE dedup_key LIKE 'report:%'") == 1   # письмо с вложениями
    test_vk_comments_pipeline_and_alert(ctx)
    r1 = run(builder.build(ctx, hours=2, send=False))
    assert r1["items"] >= 1
    d = docx.Document(r1["docx"])
    text = "\n".join(p.text for p in d.paragraphs)
    assert "Что произошло" in text and "Покрытие" in text and "Инциденты" in text
    wb1 = openpyxl.load_workbook(r1["xlsx"])
    ws = wb1["Упоминания"]
    assert ws["A2"].value == "P1" and ws["L2"].hyperlink is not None
    assert wb1["Инциденты"]["A2"].value == "P1" and "без реакции" in wb1["Инциденты"]["B2"].value
