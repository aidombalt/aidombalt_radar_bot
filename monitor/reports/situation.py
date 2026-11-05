"""«Состояние» для отчёта: не только что нашлось за период, но и что ОТКРЫТО сейчас.

Урок пилота 24–25.09: вечером отключили свет в половине Мурино (наш ЖК «Цвета Радуги» в списке), алерт P2 ушёл
вовремя, а утренний отчёт написал «упоминаний нет» — период начался после инцидента. Теперь отчёт всегда
начинается с положения дел: открытые инциденты со статусом (кто взял, когда, сколько ждали), что появилось
после последнего инцидента, сюжеты в районах (волна публикаций по одной теме), нерабочее время, дыры в покрытии.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .. import workhours
from ..util import MSK, clip, iso, jload, msk, now_utc, parse_iso

if TYPE_CHECKING:
    from ..context import Ctx

LOOKBACK_DAYS = 7
WF_TEXT = {"open": "без реакции", "in_work": "в работе", "resolved": "закрыт", "escalated": "эскалирован"}
TOPIC_TITLES = {"электричество": "отключения света", "горячая_вода": "горячая вода", "холодная_вода": "вода",
                "отопление": "отопление", "лифт": "лифты", "мусор": "мусор", "уборка": "уборка",
                "протечки_кровля": "протечки", "подвал_канализация": "канализация", "смена_УК_ОСС": "смена УК, собрания",
                "суд_прокуратура_надзор": "прокуратура и надзор", "тарифы_начисления": "платёжки и тарифы",
                "безопасность": "безопасность", "двор_благоустройство": "двор и благоустройство",
                "парковка_шлагбаум": "парковка", "снег_наледь": "снег и наледь", "благодарность": "благодарности",
                "охрана_доступ": "охрана и доступ", "домофон": "домофоны", "капремонт": "капремонт",
                "грызуны_насекомые": "грызуны и насекомые", "диспетчерская_связь": "диспетчерская"}
MIN_STORY = 3          # сюжет — от трёх публикаций (или любая тема, где был наш инцидент)


def plural(n: int, one: str, few: str, many: str) -> str:
    w = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {w}"


def pubs(n: int) -> str:
    return plural(n, "публикация", "публикации", "публикаций")


def dur(td: timedelta) -> str:
    m = int(td.total_seconds() // 60)
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} ч {m} мин" if m else f"{h} ч"
    return f"{h // 24} дн {h % 24} ч"


def off_hours(ctx: "Ctx", ts: str | None) -> bool:
    return bool(ts) and workhours.is_off(ctx, ts)


def weak_objects(ctx: "Ctx", item: dict) -> list[str]:
    """Наши объекты из текста — в том числе неуверенные совпадения (для подписи «что затронуто»)."""
    tags = jload(item.get("tags"), []) or []
    out = [t.replace(":", " ", 1) for t in tags if t.startswith(("ЖК:", "УК:"))]     # «ЖК Цвета Радуги», «УК «Этажи»»
    if not out:
        m = jload(item.get("match_json"), {}) or {}
        out = sorted({f"ЖК {ctx.reg.zhk_name(h['zhk'])}" for h in m.get("hits", [])
                      if h.get("zhk") and h["kind"] == "zhk" and h["conf"] >= 0.3})
    return out


def cluster_of(ctx: "Ctx", item: dict) -> str:
    tags = jload(item.get("tags"), []) or []
    cl = next((t.split(":", 1)[1] for t in tags if t.startswith("кластер:")), "")
    if cl:
        return cl
    objs = weak_objects(ctx, item)
    return objs[0] if objs else "прочее"


def topic_of(item: dict) -> str:
    a = item.get("_a") or jload(item.get("analysis_json"), {}) or {}
    tops = [t for t in a.get("topics", []) if t not in ("прочее", "благодарность")]
    return tops[0] if tops else "прочее"


def story_label(cluster: str, topic: str) -> str:
    return f"{cluster} · {TOPIC_TITLES.get(topic, topic.replace('_', ' '))}"


# ------------------------------------------------------------------------------------------ открытые инциденты
def matters(ctx: "Ctx", p_from: str, p_to: str) -> list[dict]:
    """Алерты P1/P2 за неделю: открытые — всегда, закрытые — если закрыты за период. Со статусом и хронологией."""
    db, out = ctx.db, []
    for a in db.q("SELECT * FROM alerts WHERE created_at>=? AND created_at<=? ORDER BY created_at DESC",
                  (iso(now_utc() - timedelta(days=LOOKBACK_DAYS)), p_to)):
        it = db.item(a["item_id"])
        if not it or it["workflow"] == "false_positive":
            continue
        fb = db.one("SELECT user, ts, verdict FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (it["id"],))
        wf = it["workflow"] or "open"
        if wf == "resolved" and not (fb and fb["ts"] >= p_from):
            continue                                   # закрыт раньше периода — в отчёте не нужен
        if it["priority"] not in ("P1", "P2") and wf != "resolved":
            continue                                   # потом понижен — не инцидент
        an = jload(it["analysis_json"], {}) or {}
        src = db.one("SELECT title FROM sources WHERE key=?", (it["source_key"],))
        created, acked = parse_iso(a["created_at"]), parse_iso(a["acked_at"]) if a["acked_at"] else None
        now = min(parse_iso(p_to) or now_utc(), now_utc())
        if wf == "in_work":
            status = f"в работе у {a['acked_by'] or (fb['user'] if fb else '—')} с {msk(a['acked_at'] or (fb and fb['ts']))}"
            if acked and now - acked > timedelta(hours=24):     # висит давно — мягко напомнить закрыть
                status += f" (уже {dur(now - acked)}; если решено — нажмите «Решено»)"
        elif wf == "resolved":
            status = f"закрыт ({fb['user']}, {msk(fb['ts'])})" if fb else "закрыт"
        else:
            status = f"без реакции {dur(now - created)}" + (f", эскалаций: {a['escalations']}" if a["escalations"] else "")
        mets = db.q("SELECT ts, views, likes, comments FROM metrics WHERE item_id=? ORDER BY ts", (it["id"],))
        views0 = mets[0]["views"] if mets else it["views"]
        out.append({
            "id": it["id"], "priority": a["priority"] if wf != "resolved" else it["priority"], "workflow": wf,
            "status": status, "summary": an.get("summary") or clip(it["text"], 200), "action": an.get("action", ""),
            "reply": an.get("reply_draft", ""), "url": it["url"], "source": src["title"] if src else it["source_key"],
            "published_at": it["published_at"], "alerted_at": a["created_at"], "acked_at": a["acked_at"],
            "acked_by": a["acked_by"], "reaction": dur(acked - created) if acked and created else "",
            "waiting": dur(now - created) if wf == "open" and created else "",
            "off_hours": off_hours(ctx, it["published_at"]), "objects": weak_objects(ctx, it),
            "cluster": cluster_of(ctx, it), "topic": topic_of(it),
            "views": it["views"] or 0, "views_first": views0 or 0, "n_metrics": len(mets),
            "followers": it["author_followers"] or 0, "in_period": it["published_at"] >= p_from,
        })
    return out


# ------------------------------------------------------------------------------------------ сюжеты
def stories(ctx: "Ctx", items: list[dict], mats: list[dict]) -> list[dict]:
    """Одна тема в одном районе, несколько публикаций = сюжет («Мурино · отключения света»: 9 публикаций)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        groups[(cluster_of(ctx, it), topic_of(it))].append(it)
    mat_keys = {(m["cluster"], m["topic"]): m for m in mats}
    out = []
    for (cl, tp), lst in groups.items():
        if tp == "прочее" or (len(lst) < MIN_STORY and (cl, tp) not in mat_keys):
            continue
        lst.sort(key=lambda x: x["published_at"] or "")
        objs = sorted({o for it in lst for o in weak_objects(ctx, it)})
        # официальные ПОСТЫ (администрация, надзор) — первыми: в них причина и сроки; комментарии у губернатора — после
        official = [it for it in lst if it.get("_cat") in ("official", "governor") and it["platform"] != "vk_comment"]
        media = [it for it in lst if it.get("_cat") == "media"]
        facts = []
        for it in (official[:2] + media[:1] + lst[-1:]):
            s = (it.get("_a") or {}).get("summary") or clip(it["text"], 160)
            if s not in [f["text"] for f in facts]:
                facts.append({"text": clip(s, 220), "source": it["_source"], "url": it["url"], "at": it["published_at"]})
        m = mat_keys.get((cl, tp))
        out.append({"label": story_label(cl, tp), "cluster": cl, "topic": tp, "n": len(lst),
                    "sources": list(dict.fromkeys(it["_source"] for it in lst))[:6],
                    "first": lst[0]["published_at"], "last": lst[-1]["published_at"], "objects": objs,
                    "reach": sum(int(it.get("author_followers") or 0) for it in lst), "facts": facts[:4],
                    "matter": m["id"] if m else None,
                    "after_matter": sum(1 for it in lst if m and it["published_at"] > m["published_at"] and it["id"] != m["id"]),
                    "officials": [it["_source"] for it in official][:3]})
    out.sort(key=lambda s: (s["matter"] is None, not s["objects"], -s["n"]))
    return out


# ------------------------------------------------------------------------------------------ покрытие
def coverage(ctx: "Ctx", d: dict) -> list[str]:
    notes = []
    gn = ctx.db.kv_get("gnews_pause", {}) or {}
    if gn.get("until") and gn["until"] > iso():
        notes.append(f"Google News ограничивает запросы с {msk(gn.get('since'))} (пауза до {msk(gn['until'])}): "
                     "издания без RSS в этот период могли не попасть; Telegram, ВК и RSS работают.")
    elif gn.get("ok_at") and gn["ok_at"] >= d["from"]:
        notes.append(f"Google News был недоступен часть периода, работает снова с {msk(gn['ok_at'])}.")
    for w in ctx.db.kv_get("downtimes", []) or []:
        a, b = parse_iso(w["from"]), parse_iso(w["to"])
        if a and b and w["to"] > d["from"] and w["from"] < d["to"] and b - a >= timedelta(minutes=30):
            notes.append(f"Радар не работал {dur(b - a)} ({msk(w['from'])} — {msk(w['to'])}): ноутбук спал, выключался "
                         "или Радар перезапускали. Пропущенное дочитано, но алерты за это время пришли с опозданием.")
    if not ctx.brain.colab_online and d.get("colab_share", 0) == 0:
        notes.append("ℹ️ Colab выключен — публикации разбирала локальная модель на ноутбуке.")
    if d["failing"]:
        names = ", ".join((s["title"] or s["key"]) for s in d["failing"][:3])
        notes.append(f"Не отвечают {len(d['failing'])} из {d['sources_enabled']} площадок: {names}"
                     + (" и др." if len(d["failing"]) > 3 else "."))
    return notes


# ------------------------------------------------------------------------------------------ заголовок
def _where(m: dict) -> str:
    objs = ", ".join(m["objects"][:2])
    return f"{objs} ({m['cluster']})" if objs and m["cluster"] not in objs else objs or m["cluster"]


def headline(ctx: "Ctx", d: dict) -> dict:
    """Детерминированные первые строки отчёта — модель их не переписывает, только продолжает.
    «После последнего инцидента (что, где, статус) новых упоминаний не было» — чтобы утро после вечернего ЧП
    не выглядело идеальным."""
    mats, ours = d["matters"], d["ours"]
    open_ = [m for m in mats if m["workflow"] in ("open", "in_work", "escalated")]
    waiting = [m for m in open_ if m["workflow"] == "open"]
    new_ours = [it for it in ours if it["published_at"] >= d["from"]]
    topic = lambda m: TOPIC_TITLES.get(m["topic"], m["topic"].replace("_", " "))  # noqa: E731
    one = open_[0] if len(open_) == 1 else None
    if any(m["priority"] == "P1" for m in waiting):
        level, icon = "red", "🔴"
        m = next(m for m in waiting if m["priority"] == "P1")
        text = f"Требует реакции: P1 · {_where(m)} · {topic(m)} — без ответа {m['waiting']}."
    elif one:
        level, icon = ("red" if one["workflow"] == "open" and one["priority"] == "P1" else "orange"), "🟠"
        text = f"Открыт инцидент {one['priority']} · {_where(one)} · {topic(one)} — {one['status']}."
    elif open_:
        level, icon = "orange", "🟠"
        text = (f"Открыто {plural(len(open_), 'инцидент', 'инцидента', 'инцидентов')}: в работе "
                f"{sum(1 for m in open_ if m['workflow'] == 'in_work')}" + (f", без реакции {len(waiting)}" if waiting else "") + ".")
    elif new_ours:
        level, icon = "yellow", "🟡"
        text = f"Срочного нет; упоминаний наших объектов для контроля: {len(new_ours)}."
    else:
        level, icon = "green", "🟢"
        text = "Спокойно: новых упоминаний наших объектов нет, открытых инцидентов нет."
    after = ""
    last = max(mats, key=lambda m: m["published_at"], default=None)
    if last:
        later = [it for it in ours if it["published_at"] > last["published_at"] and it["id"] != last["id"]]
        st = next((s for s in d.get("stories", []) if s["matter"] == last["id"]), None)
        tail = ("новых упоминаний наших объектов не было" if not later else
                f"появилось ещё {plural(len(later), 'упоминание', 'упоминания', 'упоминаний')} наших объектов (ниже)")
        if st and st["after_matter"]:
            who = ", ".join(x for x in (st["officials"][:1] + [s_ for s_ in st["sources"] if s_ not in st["officials"]][:2]))
            tail += f"; по теме вышло ещё {pubs(st['after_matter'])} ({who})"
        if last is one:
            after = f"После него ({msk(last['published_at'])}) {tail}."
        else:
            after = (f"После последнего инцидента — «{clip(last['summary'], 140)}» ({last['priority']}, "
                     f"{msk(last['published_at'])}, {_where(last)}; {last['status']}) — {tail}.")
    off = [m for m in mats if m["off_hours"] and m["in_period"]]
    off_txt = ""
    if off:
        m = off[0]
        off_txt = (f"Случилось в нерабочее время ({msk(m['published_at'])})"
                   + (f"; взят в работу через {m['reaction']}." if m["reaction"] else "; в работу пока не взят."))
    return {"level": level, "icon": icon, "text": text, "after": after, "off_hours": off_txt}


def decorate(ctx: "Ctx", rows) -> list[dict]:
    out = []
    for r in rows:
        it = dict(r)
        it["_a"] = jload(it["analysis_json"], {}) or {}
        it["_tags"] = jload(it["tags"], []) or []
        src = ctx.db.one("SELECT title, category FROM sources WHERE key=?", (it["source_key"],))
        it["_source"] = src["title"] if src else it["source_key"]
        it["_cat"] = src["category"] if src else ""
        out.append(it)
    return out


def matter_wave(ctx: "Ctx", m: dict) -> list[dict]:
    """Публикации по теме открытого инцидента с его начала — даже если они были до периода отчёта."""
    start = iso((parse_iso(m["published_at"]) or now_utc()) - timedelta(hours=3))
    rows = ctx.db.q("SELECT * FROM items WHERE published_at>=? AND relevant=1 AND priority IN ('P1','P2','P3') "
                    "AND tags LIKE ?", (start, f"%тема:{m['topic']}%"))
    return [it for it in decorate(ctx, rows) if cluster_of(ctx, it) == m["cluster"] and topic_of(it) == m["topic"]]


def enrich(ctx: "Ctx", d: dict) -> dict:
    for it in d["items"]:
        it["_story"] = story_label(cluster_of(ctx, it), topic_of(it))
        it["_objs"] = weak_objects(ctx, it) + [t.split(":", 1)[1] for t in it["_tags"] if t.startswith("сосед:")]
    d["matters"] = matters(ctx, d["from"], d["to"])
    d["metrics"] = {m["id"]: [dict(r) for r in ctx.db.q(
        "SELECT ts, views, likes, comments, reposts FROM metrics WHERE item_id=? ORDER BY ts", (m["id"],))]
        for m in d["matters"]}
    pool = {it["id"]: it for it in d["items"]}
    for m in d["matters"]:
        if m["workflow"] != "resolved":
            for it in matter_wave(ctx, m):
                pool.setdefault(it["id"], it)
    d["stories"] = stories(ctx, list(pool.values()), d["matters"])
    d["head"] = headline(ctx, d)
    d["coverage_notes"] = coverage(ctx, d)
    d["off_hours_items"] = [it for it in d["items"] if off_hours(ctx, it["published_at"])]
    return d


def note_icon(n: str) -> str:
    """ℹ️ — к сведению (Colab выключен — это штатно), ⚠️ — дыра в покрытии."""
    return n if n.startswith("ℹ️") else f"⚠️ {n}"


def state_lines(d: dict) -> list[str]:
    h = d["head"]
    return [f"{h['icon']} {h['text']}", *(x for x in (h["after"], h["off_hours"]) if x)]


def now_msk() -> datetime:
    return datetime.now(MSK)
