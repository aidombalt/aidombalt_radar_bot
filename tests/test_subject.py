"""«Наш ЖК упомянут» ≠ «наша проблема». 29.09: пост о потерявшемся коте у ЖК Виктория ушёл как P2 с черновиком ответа
от УК. Срочным остаётся только то, за что спросят с УК или застройщика."""
from monitor.util import iso, jdump

CAT = ("Здравствуйте с лета переодически у разных домов встречают кота! Последний раз видели у ЖК Виктория на Авиаторов "
       "Балтики 27.09, в августе обитал на Менделеева 16, визуально ухоженный но уставший и очень пугливый котик, не "
       "получилось его поймать! Бегает, мяукает но не дается Пожалуйста, если вы видели его, напишите мне с любой "
       "информацией, хочется найти его до сильных холодов и найти старых или новых хозяев! Проверяйте пожалуйста машину "
       "перед запуском, кот может забраться греться под капот или колеса! Подслушано в Мурино 678")

# ровно то, что ответила модель 29.09 (см. письмо): «не содержит прямого негатива по ЖКХ, но упоминает наш объект
# и призывает к действию… безопасность жильцов»
LLM_CAT = {"relevant": True, "relation": "our", "sentiment": -1, "tone": "нейтрально", "topics": ["безопасность", "прочее"],
           "addressees": [], "author_type": "житель", "call_to_action": True, "hazard": True, "virality": "средняя",
           "priority": "P2", "summary": "сообщают о появлении кота у ЖК Виктория", "why": "призыв к действию",
           "action": "Проверить факт на объекте и ответить от лица УК", "reply_draft": "Уважаемые жильцы…",
           "incident_hint": "Виктория: прочее", "entities": []}


def run(ctx, text, analysis=None, level="local"):
    ctx.db.upsert_source({"key": "tg_channel:podslushano_v_murino", "kind": "tg_channel", "handle": "podslushano_v_murino",
                          "title": "Подслушано в Мурино", "category": "public", "region": "LO"})
    m = ctx.matcher.match(text, "", "public", "LO").to_json()
    iid = ctx.db.x("INSERT INTO items(platform, external_id, source_key, url, text, published_at, collected_at, match_json, "
                   "candidate, status) VALUES('tg',?,?,?,?,?,?,?,1,'queued')",
                   (f"tg:podslushano_v_murino/{abs(hash(text)) % 10**8}", "tg_channel:podslushano_v_murino",
                    "https://t.me/podslushano_v_murino/1", text, iso(), iso(), jdump(m)))
    if analysis is None:
        ctx.pipeline.rules_analyze(iid)
    else:
        ctx.pipeline.finalize(iid, {**LLM_CAT, **analysis}, level)
    it = ctx.db.item(iid)
    return it["priority"], bool(ctx.db.one("SELECT 1 FROM alerts WHERE item_id=?", (iid,)))


def test_lost_cat_is_not_our_business(ctx):
    assert run(ctx, CAT, {}) == ("P4", False)                                   # модель сказала P2 — всё равно P4
    assert run(ctx, CAT + " ", None) == ("P4", False)                           # и без ИИ


def test_other_landmark_mentions(ctx):
    keys = "Потеряли ключи с брелоком у ЖК Виктория, Авиаторов Балтики 9к1, нашедшего прошу вернуть"
    assert run(ctx, keys, {"hazard": False, "call_to_action": False})[0] == "P4"
    ad = "Сдам двушку в ЖК Виктория на Авиаторов Балтики, 9к1, собственник"
    assert run(ctx, ad, {"sentiment": 0, "hazard": False})[0] in ("P3", "P4")


def test_neutral_mention_is_not_urgent(ctx):
    news = "В ЖК Виктория на Авиаторов Балтики 9к1 завтра с 10 до 18 плановые работы на сетях горячей воды"
    assert run(ctx, news, {"sentiment": 0, "tone": "новость", "hazard": False, "call_to_action": False,
                           "topics": ["горячая_вода"]}) == ("P3", False)


def test_real_problems_still_wake_people(ctx):
    water = "ЖК Виктория, Авиаторов Балтики 9к1 — третий день нет горячей воды, УК не отвечает, сколько можно"
    assert run(ctx, water, {"sentiment": -2, "tone": "жалоба", "hazard": False, "topics": ["горячая_вода"]})[0] in ("P1", "P2")
    # кошки — но тема ЖКХ и претензия к УК: это наше
    cats = "В подвале ЖК Виктория на Авиаторов Балтики 9к1 живут бездомные кошки, вонь в парадной, УК не реагирует"
    assert run(ctx, cats, {"sentiment": -2, "tone": "жалоба", "hazard": False, "topics": ["подвал_канализация"]})[0] in ("P1", "P2")
    # нейтральный тон, но надзор — поднимаем
    gzhi = "Жители ЖК Виктория на Авиаторов Балтики 9к1 обратились в ГЖИ по поводу начислений"
    assert run(ctx, gzhi, {"sentiment": 0, "tone": "новость", "hazard": False, "topics": ["суд_прокуратура_надзор"]})[0] in ("P1", "P2")


def test_cta_is_mass_action_only(ctx):
    cta = lambda t: ctx.matcher.match(t, "", "public", "LO").cta                 # noqa: E731
    assert not cta("Если видели кота, напишите мне в личку")
    assert not cta("Пишите мне, отдам даром")
    assert cta("Соседи, пишите все в ГЖИ и губернатору!")
    assert cta("Пишите жалобы в прокуратуру")


def test_landmark_words_do_not_catch_complaints():
    from monitor.pipeline import landmark_only
    for t in ("охранные услуги фактически не оказывались, жильцы ЖК Виктория возмущены",
              "сниму на видео этот бардак у ЖК Виктория и отправлю губернатору",
              "котельная опять встала, в ЖК Виктория холодно",
              "который день у ЖК Виктория не вывозят мусор"):
        assert landmark_only(t.lower()) == "", t
