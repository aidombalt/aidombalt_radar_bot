"""Рекомендованный набор, наши группы УК, тёзки из разбора 24.09 и эскалационные формулировки."""
import asyncio

from monitor import linkadd as la
from monitor.util import ago, iso


def test_other_organizations_with_our_names(ctx):
    m = lambda t: ctx.matcher.match(t, "", "media", "SPB")  # noqa: E731
    assert not m("Прокуратура подала иск к УК «Петербургский дом-Центр» из-за протечек").ours
    assert not m("УК Балтийский дом в Калининграде: жители ЖК Аквамарин жалуются на отопление").ours
    assert not m("Жильцы ЖК «Цивилизация» на Дыбенко жалуются на застройщика").ours
    assert m("Жители Солнечного города получили от УК «Цивилизация» квитанции по 14 тысяч").ours
    assert m("Протечка в доме на Шувалова 38, Мурино").ours                         # Урбанист, сверено с dombalt.ru


def test_escalation_phrases(ctx):
    sk = ctx.matcher.match("Председатель СК поручил доложить: в Мурино жители дома на улице Шувалова месяц без "
                           "горячей воды", source_category="official")
    assert "sk" in sk.addressees and sk.candidate and sk.urgency == 1
    cap, _ = ctx.pipeline.evidence_cap({"platform": "tg"}, sk.to_json(), {})
    assert cap == "P3"                    # улица без дома — район, а не наш дом (с 2026.10.23; было P2)
    ctx.settings.risk.street_dense = ["Шувалова"]                                   # улица, где почти все дома наши
    assert ctx.pipeline.evidence_cap({"platform": "tg"}, sk.to_json(), {})[0] == "P2"
    oss_ = ctx.matcher.match("УК Приморский дом: жители говорят о поддельных протоколах общего собрания")
    assert oss_.negativity > 0
    from monitor.rules import confirm_topics
    assert "смена_УК_ОСС" in confirm_topics("поддельные протоколы общего собрания", [])
    assert "onf" not in ctx.matcher.match("Конфликт на конференции").addressees       # «онф» внутри слов — не ОНФ


def _own_group(ctx):
    ctx.db.upsert_source({"key": "vk_wall:ykprimorskidom", "kind": "vk_wall", "handle": "ykprimorskidom", "title": "УК ПД",
                          "tier": "warm", "category": "own", "region": "LO", "comments": True, "where": "server"})
    ctx.db.x("UPDATE sources SET meta_json=? WHERE key='vk_wall:ykprimorskidom'", ('{"company": "primdom", "zhk": "victoria"}',))
    return dict(ctx.db.one("SELECT * FROM sources WHERE key='vk_wall:ykprimorskidom'"))


def test_own_group_comments(ctx):
    src = _own_group(ctx)
    raw = lambda eid, text, author="vk:777": {"platform": "vk_comment", "external_id": eid, "author_id": author,  # noqa: E731
                                              "text": text, "published_at": iso()}
    ctx.pipeline.ingest([raw("c1", "Уважаемые жители! 25.09 плановое отключение горячей воды", "vk:-123"),
                         raw("c2", "Третий день нет горячей воды, сколько можно, никто не отвечает"),
                         raw("c3", "Нет воды неделю! Пишем губернатору и в прокуратуру, репост всем соседям")], src)
    get = lambda eid: dict(ctx.db.one("SELECT * FROM items WHERE external_id=?", (eid,)))  # noqa: E731
    assert not get("c1")["candidate"]                                                # пост самой УК — контекст
    c2, c3 = get("c2"), get("c3")
    assert c2["candidate"] and c3["candidate"]
    from monitor.util import jload
    cap2, why2 = ctx.pipeline.evidence_cap(c2, jload(c2["match_json"]), {})
    cap3, _ = ctx.pipeline.evidence_cap(c3, jload(c3["match_json"]), {})
    assert (cap2, cap3) == ("P3", "P2") and "нашей группе" in why2
    ctx.pipeline.finalize(c3["id"], {"relevant": True, "priority": "P1", "relation": "our", "sentiment": -2,
                                     "summary": "нет воды", "topics": ["горячая_вода"]}, "colab")
    it = get("c3")
    assert it["priority"] == "P2" and "ЖК:Виктория" in it["tags"] and "Приморский Дом" in it["tags"]


def test_pack_file_and_install(ctx, monkeypatch):
    pack = la.load_pack()
    assert len(pack) > 50 and all(not isinstance(la.parse(it["link"]), str) for it in pack)
    assert all(it["category"] in la.CATEGORIES and it["tier"] in la.TIERS for it in pack)
    zhks = set(ctx.reg.zhks)
    assert all(it["zhk"] in zhks for it in pack if it.get("zhk"))
    assert all(it["company"] in ctx.reg.companies for it in pack if it.get("company"))

    fresh, stale = iso(), ago(days=400)

    async def fake_preview(ctx_, service, link):
        p = la.parse(link)
        if "dead" in link:
            return la.Found("tg", p[1], link=link, kind="tg_channel", title="Мёртвый", last_at=stale)
        if "nope" in link:
            return la.Found("", "", link=link, error="Telegram не знает @nope: проверьте написание")
        if p[0] == "vk":
            return la.Found("vk", "primdom_real", link=link, kind="vk_wall", title="УК Приморский дом", uid=-42,
                            members=1500, last_at=fresh, comments=True)
        return la.Found("tg", p[1], link=link, kind="tg_channel", title="Девяткино онлайн | Мурино", members=59000,
                        last_at=fresh)
    monkeypatch.setattr(la, "preview", fake_preview)
    items = [{"link": "t.me/news9online", "category": "public", "tier": "hot", "region": "LO", "why": "Мурино"},
             {"link": "vk.com/public1", "category": "own", "tier": "warm", "region": "LO", "comments": True,
              "company": "primdom", "zhk": "victoria", "why": "наша группа"},
             {"link": "t.me/dead_channel", "category": "public", "tier": "hot", "region": "LO"},
             {"link": "t.me/nope_channel", "category": "public", "tier": "hot", "region": "LO"}]
    got = [asyncio.run(la.install_pack_item(ctx, None, it))[0] for it in items]
    assert got == ["added", "added", "dead", "error"]
    s = dict(ctx.db.one("SELECT * FROM sources WHERE key='vk_wall:primdom_real'"))
    assert s["category"] == "own" and s["comments"] == 1 and '"zhk": "victoria"' in s["meta_json"] and '"owner_id": -42' in s["meta_json"]
    assert la.pack_state(ctx, items[1])[0] == "added" and la.pack_state(ctx, items[2])[0] == "dead"
    assert la.pack_state(ctx, {"link": "t.me/news9online"})[0] in ("added", "exists")
    before = la.vk_budget(ctx)
    assert la.vk_budget(ctx, [{"tier": "warm"}]) == before + 1440 // ctx.settings.schedule.warm_minutes
