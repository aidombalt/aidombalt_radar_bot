"""Сбор данных за период → Excel (данные) + Word (записка) + письмо + короткое сообщение в Telegram.

Отчёт уходит ВСЕГДА. Первая строка — СОСТОЯНИЕ сейчас (открытые инциденты со статусом, что было после последнего),
а не только «что нашлось за период»: иначе утренний отчёт после вечернего ЧП выглядит «идеально».
Текст пишет модель (Colab или локальная) по фактам; шаблон — только если модели нет или она запаниковала.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..settings import DATA_DIR
from ..util import MSK, clip, html_escape as e, iso, jload, msk, now_utc, parse_iso
from . import excel, situation, word

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger("radar.reports")
PRIO_ORDER = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
PANIC_RE = re.compile(r"паник|катастроф|резонанс|волн\w* (?:жалоб|негатив)|угроз\w* репутац|немедленн", re.I)


def _is_ours(it: dict) -> bool:
    """«Наше»: объект/адрес назван в тексте (теги ЖК:/УК:), прислал оператор — или это P1/P2 (по нему был алерт)."""
    return (any(t.startswith(("ЖК:", "УК:")) for t in it["_tags"]) or it["platform"] == "manual"
            or it["priority"] in ("P1", "P2"))


def gather(ctx: "Ctx", p_from: str, p_to: str) -> dict:
    db = ctx.db
    # старые посты, найденные при первом опросе канала, в отчёт периода не попадают (кроме срочных)
    since = iso((parse_iso(p_from) or now_utc()) - timedelta(hours=24))
    items = [dict(r) for r in db.q(
        "SELECT * FROM items WHERE analyzed_at>? AND analyzed_at<=? AND relevant=1 AND priority IN ('P1','P2','P3') "
        "AND (published_at>=? OR priority IN ('P1','P2')) "
        "ORDER BY CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END, risk_score DESC", (p_from, p_to, since))]
    for it in items:
        it["_a"] = jload(it["analysis_json"], {}) or {}
        it["_tags"] = jload(it["tags"], []) or []
        src = db.one("SELECT title, category FROM sources WHERE key=?", (it["source_key"],))
        it["_source"] = src["title"] if src else it["source_key"]
        it["_cat"] = src["category"] if src else ""
        it["_ours"] = _is_ours(it)
    items.sort(key=lambda it: (not it["_ours"], PRIO_ORDER.get(it["priority"], 3), -(it["risk_score"] or 0)))
    ours = [it for it in items if it["_ours"]]
    background = [it for it in items if not it["_ours"]]
    # не присланы алертами: чужой корпус в нашем ЖК, копии отмеченных историй (дизайн §5.5); на пересмотр — отмеченные чужими
    skipped = []
    for r in db.q("SELECT * FROM items WHERE analyzed_at>? AND analyzed_at<=? AND (analysis_json LIKE '%\"cap_kind\": \"foreign\"%' "
                  "OR analysis_json LIKE '%\"cap_kind\": \"registry_check\"%' OR analysis_json LIKE '%\"cap_kind\": \"same_case\"%') "
                  "ORDER BY analyzed_at DESC LIMIT 300", (p_from, p_to)):
        r = dict(r)
        rk = ((jload(r["analysis_json"], {}) or {}).get("_risk") or {})
        r["_why"] = (rk.get("combine") or "").split("не выше ")[-1]
        r["_kind"] = {"foreign": "чужой дом", "registry_check": "проверить справочник", "same_case": "копия истории"}.get(rk.get("cap_kind"), "")
        skipped.append(r)
    review = [{"address": n["addresses"][0] if n.get("addresses") else "", "zhk": ctx.reg.zhk_name(n.get("zhk", "")),
               "note": n.get("note", ""), "learned": bool(n.get("learned"))} for n in ctx.reg.neighbors if n.get("zhk")]
    tot = db.one("SELECT COUNT(*) n, SUM(candidate) c FROM items WHERE collected_at>? AND collected_at<=?", (p_from, p_to))
    levels = Counter(r["analysis_level"] for r in db.q(
        "SELECT analysis_level FROM items WHERE analyzed_at>? AND analyzed_at<=?", (p_from, p_to)))
    runs = db.one("SELECT COUNT(*) n, SUM(n_err) err FROM runs WHERE started_at>? AND started_at<=?", (p_from, p_to))
    sources = [dict(r) for r in db.q("SELECT * FROM sources WHERE candidate=0 ORDER BY enabled DESC, tier, key")]
    # площадки и поисковые запросы считаем отдельно: 100 запросов «ещё не опрошены» — это расписание, а не сбой
    real = [s for s in sources if s["enabled"] and s["kind"] not in ("vk_search", "gnews", "manual")]
    searches = [s for s in sources if s["enabled"] and s["kind"] in ("vk_search", "gnews")]
    alive = lambda s: s["last_ok"] and s["last_ok"] >= p_from and (s["consecutive_failures"] or 0) < 3  # noqa: E731
    ok = [s for s in real if alive(s)]
    failing = [s for s in real if (s["consecutive_failures"] or 0) >= 3 or (not s["last_ok"] and s["last_error"])]
    waiting = [s for s in real if not s["last_ok"] and not s["last_error"]]
    tags = Counter(t for it in ours for t in it["_tags"] if not t.startswith(("приоритет:", "площадка:")))
    bg_topics = Counter(t.split(":", 1)[1] for it in background for t in it["_tags"] if t.startswith("тема:"))
    objects = Counter(o for it in ours for o in situation.weak_objects(ctx, it))
    # прошлый такой же период — для сравнения «больше/меньше обычного»
    span = (parse_iso(p_to) or now_utc()) - (parse_iso(p_from) or now_utc())
    prev_from = iso((parse_iso(p_from) or now_utc()) - span)
    prev = db.one("SELECT SUM(priority IN ('P1','P2')) urgent, SUM(priority='P3') p3, COUNT(*) n FROM items "
                  "WHERE analyzed_at>? AND analyzed_at<=? AND relevant=1 AND priority IN ('P1','P2','P3')", (prev_from, p_from))
    d = {"skipped": skipped, "foreign_review": review,
        "from": p_from, "to": p_to, "items": items, "ours": ours, "background": background,
        "collected": tot["n"] or 0, "candidates": tot["c"] or 0,
        "p": Counter(it["priority"] for it in ours), "levels": levels, "runs": runs["n"] or 0, "run_errors": runs["err"] or 0,
        "sources": sources, "sources_enabled": len(real), "sources_ok": len(ok), "failing": failing, "waiting": waiting,
        "searches": len(searches), "tags": tags, "objects": objects, "bg_topics": bg_topics,
        "colab_share": round(100 * levels.get("colab", 0) / max(1, sum(levels.values()))),
        "prev": {"urgent": prev["urgent"] or 0, "p3": prev["p3"] or 0, "n": prev["n"] or 0},
        "hourly": Counter((msk(it["published_at"], "%d.%m %H:00"), "ours" if it["_ours"] else "bg") for it in items),
    }
    return situation.enrich(ctx, d)


def facts_text(d: dict) -> str:
    """Факты для модели: состояние, инциденты, сюжеты, фон, покрытие. Модель пишет по ним живой текст."""
    lines = [f"Период: {msk(d['from'], '%d.%m.%Y %H:%M')} — {msk(d['to'], '%d.%m.%Y %H:%M')} МСК.",
             "СОСТОЯНИЕ (уже написано в отчёте первой строкой, не противоречь ему): " + " ".join(situation.state_lines(d))]
    for m in d["matters"]:
        lines.append(f"ИНЦИДЕНТ {m['priority']} [{msk(m['published_at'])}{', нерабочее время' if m['off_hours'] else ''}] "
                     f"{m['source']}: {m['summary']} | объекты: {', '.join(m['objects']) or m['cluster']} | статус: {m['status']}"
                     + (f" | реакция через {m['reaction']}" if m["reaction"] else "")
                     + (f" | просмотры {m['views_first']}→{m['views']}" if m["views"] > m["views_first"] else ""))
    for it in d["ours"]:
        if it["priority"] == "P3":
            lines.append(f"- P3 [{msk(it['published_at'])}] {it['_source']}: {it['_a'].get('summary') or clip(it['text'], 200)}")
    for s in d["stories"][:6]:
        lines.append(f"СЮЖЕТ «{s['label']}»: {situation.pubs(s['n'])} {msk(s['first'])}–{msk(s['last'])}; источники: "
                     f"{', '.join(s['sources'][:5])}; наши объекты упомянуты: {', '.join(s['objects']) or 'нет'}"
                     + (f"; после инцидента ещё {situation.pubs(s['after_matter'])}" if s["matter"] else "")
                     + "; факты: " + " / ".join(f["text"] for f in s["facts"][:3]))
    if d["background"]:
        lines.append(f"[ФОН — НЕ ПРО НАШИ ОБЪЕКТЫ] публикаций о ЖКХ в районах присутствия: {len(d['background'])}; темы: "
                     + ", ".join(f"{k}×{v}" for k, v in d["bg_topics"].most_common(6)))
    lines.append(f"Прошлый такой же период: срочных {d['prev']['urgent']}, для контроля {d['prev']['p3']}.")
    for n in d["coverage_notes"]:
        lines.append(f"ПОКРЫТИЕ: {n.removeprefix('ℹ️ ')}")
    return "\n".join(lines)


def template_narrative(d: dict) -> str:
    """Запасной текст без модели — из тех же фактов."""
    parts = []
    for m in d["matters"][:3]:
        st = next((s for s in d["stories"] if s["matter"] == m["id"]), None)
        parts.append(f"{msk(m['published_at'])} — {m['summary']}"
                     + (f" Охват поста вырос с {m['views_first']} до {m['views']} просмотров." if m["views"] > m["views_first"] else "")
                     + (f" По теме «{st['label']}» всего {situation.pubs(st['n'])}; главное: {st['facts'][0]['text']}"
                        if st and st["facts"] else "")
                     + (f" Что сделать: {m['action']}" if m["action"] and m["workflow"] != "resolved" else ""))
    for s in d["stories"][:3]:
        if s["matter"]:
            continue
        parts.append(f"Сюжет «{s['label']}»: {situation.pubs(s['n'])} ({', '.join(s['sources'][:3])})"
                     + (f"; упомянуты {', '.join(s['objects'])}." if s["objects"] else "; наши объекты не названы."))
    p3 = [it for it in d["ours"] if it["priority"] == "P3"]
    if p3:
        parts.append(f"Для контроля (P3): {len(p3)} упоминаний наших объектов — "
                     + ", ".join(f"{k} ({v})" for k, v in d["objects"].most_common(4)) + ".")
    if d["background"] and not d["stories"]:
        topics = ", ".join(k.replace("_", " ") for k, _ in d["bg_topics"].most_common(4)) or "разное"
        parts.append(f"Фон: {len(d['background'])} публикаций о ЖКХ в районах присутствия (темы: {topics}); "
                     "наши объекты в них не названы.")
    if not d["matters"]:
        parts.append("Действий не требуется." if not p3 else "Срочных действий не требуется; при повторах — проверить факты.")
    return " ".join(parts) or "За период значимых публикаций не было."


def email_html(d: dict, narrative: str, public_url: str = "") -> str:
    h = d["head"]
    color = {"red": "#c62828", "orange": "#ef6c00", "yellow": "#9e7c00", "green": "#2e7d32"}[h["level"]]
    state = "".join(f"<p style='margin:4px 0'>{e(x)}</p>" for x in situation.state_lines(d)[1:])
    rows = "".join(
        f"<tr><td style='padding:4px 8px'><b>{m['priority']}</b></td><td style='padding:4px 8px'>{msk(m['published_at'])}"
        f"{'<br><small>вне раб. времени</small>' if m['off_hours'] else ''}</td>"
        f"<td style='padding:4px 8px'>{e(', '.join(m['objects']) or m['cluster'])}</td>"
        f"<td style='padding:4px 8px'>{e(clip(m['summary'], 170))}</td><td style='padding:4px 8px'>{e(m['status'])}</td>"
        f"<td style='padding:4px 8px'><a href='{e(m['url'] or '')}'>ссылка</a></td></tr>" for m in d["matters"][:10])
    stories = "".join(
        f"<li><b>{e(s['label'])}</b> — {s['n']} публ., {msk(s['first'], '%d.%m %H:%M')}–{msk(s['last'], '%H:%M')}"
        f"{'; наши: ' + e(', '.join(s['objects'])) if s['objects'] else ''}; {e(', '.join(s['sources'][:3]))}</li>"
        for s in d["stories"][:5])
    cover = "".join(f"<p style='color:{'#777' if n.startswith('ℹ️') else '#c62828'};margin:2px 0'>"
                    f"{e(situation.note_icon(n))}</p>" for n in d["coverage_notes"])
    return f"""<div style="font-family:Arial,sans-serif;max-width:780px">
<h2 style="margin-bottom:4px">Радар упоминаний · {msk(d['from'], '%d.%m %H:%M')} — {msk(d['to'], '%d.%m %H:%M')} МСК</h2>
<div style="border-left:6px solid {color};padding:6px 12px;background:#fafafa">
<p style="font-size:16px;margin:4px 0"><b>{e(h['icon'] + ' ' + h['text'])}</b></p>{state}</div>
<h3>Коротко</h3><p>{e(narrative[:2200]).replace(chr(10), '<br>')}</p>
{f"<h3>Инциденты</h3><table style='border-collapse:collapse;font-size:13px' border='1'>{rows}</table>" if rows else ''}
{f"<h3>Сюжеты в районах</h3><ul>{stories}</ul>" if stories else ''}
<p style="color:#555;font-size:13px"><b>За период:</b> про наши объекты {len(d['ours'])} (P1 {d['p'].get('P1', 0)} ·
P2 {d['p'].get('P2', 0)} · P3 {d['p'].get('P3', 0)}), фон по районам {len(d['background'])}, собрано {d['collected']};
площадок работает {d['sources_ok']}/{d['sources_enabled']}.</p>{cover}
<p style="color:#777;font-size:12px">Во вложении: Excel — данные (инциденты, сюжеты, все упоминания, динамика, площадки);
Word — аналитическая записка. {f'<a href="{e(public_url)}">Открыть Радар</a>' if public_url else ''}</p></div>"""


def tg_short(d: dict, narrative: str) -> str:
    lines = [f"📊 <b>Отчёт {msk(d['from'], '%d.%m %H:%M')}–{msk(d['to'], '%H:%M')}</b>",
             *(e(x) for x in situation.state_lines(d))]
    for s in d["stories"][:2]:
        if not s["matter"]:
            lines.append(f"🗞 {e(s['label'])}: {s['n']} публ." + (f" (упомянуты {e(', '.join(s['objects']))})" if s["objects"] else ""))
    lines.append("")
    lines.append(e(clip(narrative, 900)))
    lines += [e(situation.note_icon(n)) for n in d["coverage_notes"][:2]]
    return "\n".join(lines)


def report_event(ctx: "Ctx", kind: str) -> str:
    """Какой подписке принадлежит отчёт: недельный; утренний (первый слот расписания); остальные — дневные."""
    if kind == "weekly":
        return "rep_weekly"
    slots = sorted(ctx.settings.schedule.reports or [])
    now = datetime.now(MSK)
    if kind == "regular" and slots:
        h, m = map(int, slots[0].split(":"))
        if abs((now.hour * 60 + now.minute) - (h * 60 + m)) <= 30:
            return "rep_morning"
    return "rep_day"


async def build(ctx: "Ctx", hours: float | None = None, p_from: str | None = None, kind: str = "regular",
                send: bool = True, router=None) -> dict:
    p_to = iso()
    if not p_from:
        last = ctx.db.one("SELECT period_to FROM reports WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        p_from = iso(now_utc() - timedelta(hours=hours)) if hours else (last["period_to"] if last else iso(now_utc() - timedelta(hours=24)))
    d = gather(ctx, p_from, p_to)
    narrative, who = "", ""
    if router is not None and (d["items"] or d["matters"]):
        try:
            narrative, who = await router.write_narrative(facts_text(d))
        except Exception as ex:
            log.warning("нарратив: %s", ex)
    narrative = re.sub(r"\*\*|__|^#+\s*", "", narrative or "", flags=re.M).strip()
    if narrative and (narrative[:1] in "{[" or '"priority"' in narrative or len(re.findall(r"[а-яё]", narrative, re.I)) < 40):
        log.info("нарратив модели отброшен: не похож на связный текст")
        narrative, who = "", ""
    if narrative and not any(m["priority"] in ("P1", "P2") for m in d["matters"]) and PANIC_RE.search(narrative):
        log.info("нарратив модели отброшен: тревожные формулировки при спокойной обстановке")
        narrative, who = "", ""
    if not narrative:
        narrative, who = template_narrative(d), ""
    stamp = datetime.now(MSK).strftime("%Y-%m-%d_%H%M%S")
    out_dir = DATA_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx = out_dir / f"Радар_данные_{stamp}.xlsx"
    docx = out_dir / f"Радар_записка_{stamp}.docx"
    excel.write(d, xlsx, narrative)
    word.write(d, docx, narrative, who or "шаблон")
    html = email_html(d, narrative, ctx.settings.public_url)
    rid = ctx.db.x("INSERT INTO reports(kind, period_from, period_to, created_at, xlsx_path, docx_path, n_items, summary) "
                   "VALUES(?,?,?,?,?,?,?,?)", (kind, p_from, p_to, iso(), str(xlsx), str(docx), len(d["items"]), narrative[:2000]))
    lvl = {"red": "ТРЕБУЕТ РЕАКЦИИ", "orange": "открыт инцидент", "yellow": "для контроля", "green": "спокойно"}[d["head"]["level"]]
    subj = f"Радар: {lvl} · {msk(p_from, '%d.%m %H:%M')}–{msk(p_to, '%H:%M')}"
    short = tg_short(d, narrative)
    if send:
        event = report_event(ctx, kind)
        for mail in ctx.alerts.recipients(event, "mail"):
            ctx.alerts.enqueue("email", mail, {"to": [mail], "subject": subj, "html": html,
                                               "attachments": [str(xlsx), str(docx)]}, f"report:{rid}:{mail}")
        for chat in ctx.alerts.recipients(event, "tg"):
            ctx.alerts.enqueue("telegram", chat, {"chat_id": chat, "text": short, "parse_mode": "HTML"}, f"reptg:{rid}:{chat}")
        from ..alerts.render import Card
        for chat in ctx.alerts.recipients(event, "max"):
            ctx.alerts.max.enqueue_text(chat, Card(short), f"repmax:{rid}:{chat}",
                                        files=[str(xlsx), str(docx)] if ctx.settings.max.report_files else [])
        ctx.db.x("UPDATE reports SET sent=1 WHERE id=?", (rid,))
    return {"id": rid, "xlsx": str(xlsx), "docx": str(docx), "items": len(d["items"]), "narrative_by": who or "шаблон",
            "short": short, "subject": subj, "html": html, "level": d["head"]["level"]}
