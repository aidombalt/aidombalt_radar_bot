"""Демо-отчёт на ВЫМЫШЛЕННЫХ примерах: показывает формат Excel/Word без реальных данных.
Запуск: python tools/demo.py [папка]"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["RADAR_DATA_DIR"] = tempfile.mkdtemp(prefix="radar_demo_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.context import Ctx  # noqa: E402
from monitor.reports import builder  # noqa: E402
from monitor.util import ago  # noqa: E402

DEMO = [
    # (источник, платформа, автор, подписчики, текст, анализ)
    ("vk_wall:drozdenko_a", "vk_comment", "Житель Мурино (демо)", 12400,
     "Александр Юрьевич! ЖК Виктория, Мурино, Авиаторов Балтики 9к1 — лифт стоит вторую неделю, УК не отвечает. "
     "Сколько можно?! Пишите все губернатору!",
     dict(relevant=True, relation="our", sentiment=-2, tone="жалоба", topics=["лифт", "диспетчерская_связь"],
          priority="P1", addressees=["губернатор ЛО"], author_type="инфлюенсер", call_to_action=True, virality="высокая",
          summary="Жалоба губернатору ЛО: в ЖК «Виктория» (Авиаторов Балтики 9 к1) лифт не работает две недели, диспетчер не отвечает.",
          why="Адресовано губернатору, у автора 12 тыс. подписчиков, есть призыв писать массово — высокий риск поручения и проверки.",
          action="В течение 30 минут: проверить статус ремонта лифта, ответить в комментарии от имени УК «Балтийский Дом» со сроком, связаться с автором лично.",
          reply_draft="Здравствуйте! Приносим извинения за неудобства. Заявка по лифту в д. 9 к. 1 взята на личный контроль директора УК, специалисты подрядчика на объекте. Сообщим о запуске здесь же.",
          incident_hint="ЖК Виктория: лифт")),
    ("tg_channel:tmurino", "tg", "Горячие новости Мурино I Девяткино", 24100,
     "Жители домов УК «КоммуналСервис» на Охтинской аллее пишут губернатору: не вывозят мусор. В комментариях соседи с "
     "Шувалова жалуются на своих управляющих.",
     dict(relevant=True, relation="neighbor", sentiment=-1, tone="жалоба", topics=["мусор", "двор_благоустройство"],
          priority="P2", addressees=["губернатор ЛО"], author_type="СМИ", virality="средняя",
          summary="Локальный канал Мурино тиражирует жалобу на соседнюю УК губернатору; в комментариях подтягиваются жители Шувалова.",
          why="Риск переноса: поручение по кварталу и рейд Госжилнадзора затронут наши дома на Шувалова 40–50.",
          action="Проверить вывоз мусора в ЖК «Цвета Радуги» сегодня; подготовить фотоотчёт на случай запроса.",
          incident_hint="Мурино/соседи: мусор")),
    ("vk_wall:kudrovo24", "vk_comment", "Жительница Кудрово (демо)", 40,
     "ЖК Прогресс, Европейский 13 к4 — третий день нет горячей воды, УК молчит!",
     dict(relevant=True, relation="our", sentiment=-2, tone="жалоба", topics=["горячая_вода"], priority="P2",
          summary="В ЖК «Прогресс» (Европейский 13 к4, Кудрово) третий день нет горячей воды.",
          why="Бытовой негатив в районном паблике, возможен рост в комментариях.",
          action="Проверить причину отключения, ответить в паблике от УК «Балтийский Дом» с прогнозом.",
          reply_draft="Здравствуйте! Отключение связано с аварийными работами на сетях. Ориентировочный срок подачи — сегодня до 20:00.",
          incident_hint="ЖК Прогресс: горячая вода")),
    ("rss:https://www.fontanka.ru/rss-feeds/rss.xml", "rss", "Фонтанка", 0,
     "В Мурино проверили готовность домов к отопительному сезону. Среди управляющих компаний, получивших паспорта "
     "готовности, — УК «Этажи» (ЖК Авиатор, ЖК Урбанист).",
     dict(relevant=True, relation="our", sentiment=1, tone="новость", topics=["отопление"], priority="P3",
          author_type="СМИ", summary="Фонтанка: УК «Этажи» в числе получивших паспорта готовности к отопительному сезону.",
          why="Позитив, можно использовать в коммуникации.", action="Репост в каналах УК.", incident_hint="Этажи: отопление")),
    ("vk_wall:535438957", "vk_comment", "Житель Красносельского (демо)", 150,
     "Александр Дмитриевич, Буденного 23к3 — снова прорыв трубы в подвале, вода стоит, запах. Примите меры!",
     dict(relevant=True, relation="our", sentiment=-2, tone="жалоба", topics=["подвал_канализация", "авария"],
          priority="P1", addressees=["губернатор СПб"], hazard=True,
          summary="Жалоба губернатору СПб: прорыв в подвале дома Буденного 23 к3 (ЖК «Солнечный город», УК «Цивилизация»).",
          why="Адресовано губернатору, авария с риском для здоровья — вероятна переадресация в ГЖИ.",
          action="Направить аварийную бригаду, ответить от УК «Цивилизация» в комментарии, подготовить акт.",
          reply_draft="Здравствуйте! Аварийная бригада направлена, течь локализуем. Откачка подвала и дезинфекция — сегодня.",
          incident_hint="Солнечный город: подвал")),
    ("tg_channel:tmurino", "tg", "Горячие новости Мурино I Девяткино", 24100,
     "Спасибо УК Приморский Дом — быстро починили домофон в ЖК Цвета Радуги!",
     dict(relevant=True, relation="our", sentiment=2, tone="благодарность", topics=["домофон", "благодарность"],
          priority="P3", summary="Благодарность УК «Приморский Дом» за ремонт домофона (ЖК «Цвета Радуги»).",
          why="Позитив.", action="Поблагодарить в ответ.", incident_hint="Цвета Радуги: домофон")),
]


async def main(out: Path):
    ctx = Ctx()
    ctx.sync_sources_from_yaml()
    ctx.db.upsert_source({"key": "vk_wall:535438957", "kind": "vk_wall", "handle": "535438957", "title": "Александр Беглов — ВК"})
    for i, (src, plat, author, fol, text, a) in enumerate(DEMO):
        ext = f"demo:{i}"
        ctx.pipeline.ingest([{"platform": plat, "external_id": ext, "url": f"https://example.org/demo/{i}",
                              "author_id": f"demo:{i}", "author_name": author, "author_followers": fol, "text": text,
                              "published_at": ago(hours=6 - i), "likes": 10 * (6 - i), "comments": 3 * (6 - i)}],
                            {"key": src, "category": "governor" if "535438957" in src or "drozdenko" in src else "public"})
        iid = ctx.db.one("SELECT id FROM items WHERE external_id=?", (ext,))["id"]
        ctx.pipeline.finalize(iid, {"entities": [], "reply_draft": "", "addressees": [], "author_type": "житель",
                                    "call_to_action": False, "hazard": False, "virality": "низкая", **a}, "colab")
    ctx.db.x("INSERT INTO runs(tier, started_at, finished_at, n_new, n_cand, n_err) VALUES('hot', ?, ?, 120, 9, 0)",
             (ago(hours=5), ago(hours=5)))
    narrative = ("ДЕМО-ОТЧЁТ НА ВЫМЫШЛЕННЫХ ПРИМЕРАХ.\nГлавное: два критичных обращения к губернаторам (ЖК «Виктория», "
                 "Мурино — лифт; Буденного 23 к3 — прорыв в подвале). Очаг соседского негатива (УК «КоммуналСервис») "
                 "может перейти на наши дома на Шувалова — рекомендуется превентивная проверка вывоза мусора.\n"
                 "Рекомендации: закрыть P1 ответами от УК в течение 30 минут; фотоотчёт по Шувалова; "
                 "позитив по паспортам готовности — в каналы УК.")

    class R:  # «мозг» для демо: отдаёт готовый текст
        async def write_narrative(self, facts, wait_colab_sec=0):
            return narrative, "colab"

    r = await builder.build(ctx, hours=12, kind="demo", send=False, router=R())
    out.mkdir(parents=True, exist_ok=True)
    x = out / "Пример отчёта (демо).xlsx"
    d = out / "Пример аналитической записки (демо).docx"
    shutil.copy(r["xlsx"], x)
    shutil.copy(r["docx"], d)
    print(x, d, r["items"])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
