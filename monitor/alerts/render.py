"""Тексты алертов для Telegram (HTML) и почты."""
from __future__ import annotations

from ..pipeline import PLATFORM_TITLES
from ..links import is_placeholder
from ..util import clip, html_escape as e, jload, msk

PRIO_ICON = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "⚪"}
PRIO_NAME = {"P1": "P1 · реагировать ≤30 мин", "P2": "P2 · реагировать ≤2 ч", "P3": "P3 · в дайджест", "P4": "P4 · фон"}
LEVEL = {"colab": "ИИ (Colab)", "local": "ИИ (локальный)", "rules": "⚠️ без ИИ — правила"}


def link_label(item: dict) -> str:
    """Оригинал ещё не найден (Google News закрыт в РФ) — честно пишем, что ссылка ведёт на поиск."""
    return "найти статью (поиск)" if is_placeholder(item.get("url")) else "открыть"


def objects_line(item: dict) -> str:
    tags = jload(item.get("tags"), []) or []
    objs = [t.split(":", 1)[1] for t in tags if t.startswith(("ЖК:", "УК:", "сосед:"))]
    cl = [t.split(":", 1)[1] for t in tags if t.startswith("кластер:")]
    return " · ".join(objs[:4] + cl[:1]) or "объект не определён"


def _alert_html(item: dict, update: bool = False, escalation: int = 0, source_title: str = "") -> str:
    a = jload(item.get("analysis_json"), {}) or {}
    r = a.get("_risk", {})
    tags = jload(item.get("tags"), []) or []
    head = f"{PRIO_ICON.get(item['priority'], '⚪')} <b>{PRIO_NAME.get(item['priority'], item['priority'])}</b>"
    if update:
        head = "⬆️ <b>ПОВЫШЕН ПРИОРИТЕТ</b>\n" + head
    if escalation:
        head = f"⏰ <b>ЭСКАЛАЦИЯ #{escalation}: никто не взял в работу</b>\n" + head
    who = e(item.get("author_name") or "—")
    fol = item.get("author_followers") or 0
    lines = [
        head,
        f"🏢 <b>{e(objects_line(item))}</b>",
        f"💬 {PLATFORM_TITLES.get(item['platform'], item['platform'])} · {e(source_title or item.get('source_key') or '')}",
        f"👤 {who}" + (f" ({fol:,} подп.)".replace(",", " ") if fol else ""),
        f"<blockquote>{e(clip(item.get('text') or item.get('title') or '', 600))}</blockquote>",
    ]
    if a.get("summary"):
        lines.append(f"🧠 <b>Суть:</b> {e(a['summary'])}")
    if a.get("why"):
        lines.append(f"⚠️ <b>Чем опасно:</b> {e(clip(a['why'], 400))}")
    if a.get("action"):
        lines.append(f"✅ <b>Что делать:</b> {e(clip(a['action'], 400))}")
    ht = [t.split(":", 1)[-1].replace(" ", "_") for t in tags if t.startswith(("тема:", "адресат:"))][:6]
    if ht:
        lines.append("🏷 " + " ".join(f"#{e(h)}" for h in ht))
    lines.append(f"🔗 <a href=\"{e(item.get('url') or '')}\">{link_label(item)}</a> · {msk(item.get('published_at'))} МСК · "
                 f"риск {r.get('score', item.get('risk_score', 0)):.0f}/100 · {LEVEL.get(item.get('analysis_level'), '')}")
    return "\n".join(lines)[:4000]


def _keyboard_data(item_id: int, check: bool = False, foreign: dict | None = None) -> dict:
    """check — карточка «дом не из справочника — проверить» (§6): вместо «Беру» — «🏠 Наш дом» / «🚫 Не наш».
    foreign — разобранный дом не из справочника: кнопка «🚫 <дом> — не наш дом»."""
    if check:
        lab = (foreign or {}).get("label", "дом")
        return {"inline_keyboard": [
            [{"text": f"🏠 {lab[:28]} — наш дом", "callback_data": f"h:our:{item_id}"},
             {"text": "🚫 Не наш", "callback_data": f"h:not:{item_id}"}],
            [{"text": "🟢 Решено", "callback_data": f"a:res:{item_id}"},
             {"text": "📈 Динамика", "callback_data": f"a:dyn:{item_id}"}]]}
    kb = [
        [{"text": "✅ Беру в работу", "callback_data": f"a:ack:{item_id}"},
         {"text": "🟢 Решено", "callback_data": f"a:res:{item_id}"},
         {"text": "❌ Ложное", "callback_data": f"a:fp:{item_id}"}],
        [{"text": "✍️ Черновик ответа", "callback_data": f"a:rep:{item_id}"},
         {"text": "📈 Динамика", "callback_data": f"a:dyn:{item_id}"},
         {"text": "🔥 Эскалировать", "callback_data": f"a:esc:{item_id}"}],
    ]
    if foreign:
        kb.append([{"text": f"🚫 {foreign['label'][:32]} — не наш дом", "callback_data": f"h:not:{item_id}"}])
    return {"inline_keyboard": kb}


STATE_KB_OPEN = "open"


def _card_data(item: dict, state: str = "open", who: str = "", when: str = "", note: str = "",
            source_title: str = "", foreign: dict | None = None, check: bool = False) -> tuple[str, dict]:
    """Карточка алерта в текущем состоянии. Закрытые (решено/ложное/снято) сворачиваются в 2–3 строки
    с кнопкой «вернуть» — чат не зарастает простынями, а история и отмена остаются."""
    iid = item["id"]
    stamp = f"{e(who)} · {when}" if who else when
    brief = f"#{iid} · {e(objects_line(item))} · {e(item.get('author_name') or '—')}"
    a = jload(item.get("analysis_json"), {}) or {}
    gist = e(clip(a.get("summary") or item.get("text") or "", 140))
    link = f"<a href=\"{e(item.get('url') or '')}\">{link_label(item)}</a>"
    undo = {"inline_keyboard": [[{"text": "↩️ Вернуть", "callback_data": f"a:undo:{iid}"}]]}
    if state == "false_positive":
        return (f"❌ <b>Ложное</b> · {brief}\n<i>{gist}</i>\n— {stamp} · учтено в памяти · {link}", undo)
    if state == "resolved":
        return (f"🟢 <b>Решено</b> · {brief}\n<i>{gist}</i>\n— {stamp} · {link}", undo)
    if state == "dropped":
        return (f"⚪ <b>Снято автоматически</b> · {brief}\n<i>{e(note or 'по новым правилам не срочно')}</i> · {link}", undo)
    text = _alert_html(item, source_title=source_title)
    if state == "in_work":
        text += f"\n\n🛠 <b>В работе:</b> {stamp}"
        kb = {"inline_keyboard": [
            [{"text": "🟢 Решено", "callback_data": f"a:res:{iid}"}, {"text": "❌ Ложное", "callback_data": f"a:fp:{iid}"}],
            [{"text": "✍️ Черновик ответа", "callback_data": f"a:rep:{iid}"},
             {"text": "📈 Динамика", "callback_data": f"a:dyn:{iid}"}]]}
        return text[:4000], kb
    if state == "escalated":
        text += f"\n\n🔥 <b>Эскалировано:</b> {stamp}"
    if note and state == "open":
        text += f"\n<i>{e(note)}</i>"
    return text[:4000], _keyboard_data(iid, check=check and item.get("priority") == "P2", foreign=foreign)


def email_alert_html(item: dict, source_title: str = "", public_url: str = "") -> tuple[str, str]:
    a = jload(item.get("analysis_json"), {}) or {}
    subj = f"[{item['priority']}] {objects_line(item)} — {clip(a.get('summary') or item.get('text') or '', 90)}"
    color = {"P1": "#c62828", "P2": "#ef6c00", "P3": "#f9a825"}.get(item["priority"], "#757575")
    html = f"""<div style="font-family:Arial,sans-serif;max-width:680px">
<div style="border-left:6px solid {color};padding:8px 14px;background:#fafafa">
<h2 style="margin:0;color:{color}">{e(PRIO_NAME.get(item['priority'], ''))}</h2>
<p style="margin:4px 0"><b>{e(objects_line(item))}</b> · {e(PLATFORM_TITLES.get(item['platform'], ''))} · {e(source_title)}</p></div>
<p><b>Автор:</b> {e(item.get('author_name') or '—')} ({item.get('author_followers') or 0} подп.) · {msk(item.get('published_at'))} МСК</p>
<blockquote style="border-left:3px solid #ccc;margin:0;padding:6px 12px;color:#333">{e(clip(item.get('text') or '', 1500))}</blockquote>
<p><b>Суть:</b> {e(a.get('summary', ''))}</p><p><b>Чем опасно:</b> {e(a.get('why', ''))}</p>
<p><b>Что делать:</b> {e(a.get('action', ''))}</p>
{f"<p><b>Черновик ответа:</b><br><i>{e(a.get('reply_draft'))}</i></p>" if a.get('reply_draft') else ''}
<p><a href="{e(item.get('url') or '')}">{'Найти статью (поиск Яндекса)' if is_placeholder(item.get('url')) else 'Открыть публикацию'}</a>{f' · <a href="{e(public_url)}/item/{item["id"]}">карточка в Радаре</a>' if public_url else ''}</p>
</div>"""
    return subj, html

# Channel-neutral representation. Compatibility wrappers above preserve Telegram bytes.
from dataclasses import dataclass, field
from html import unescape, escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


@dataclass
class Btn:
    text: str
    payload: str | None = None
    url: str | None = None


@dataclass
class Card:
    text_html: str
    rows: list[list[Btn]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    item_id: int | None = None

    @classmethod
    def from_tg(cls, text, kb=None, **kwargs):
        return cls(text, [[Btn(b["text"], b.get("callback_data"), b.get("url")) for b in row]
                          for row in (kb or {}).get("inline_keyboard", [])], **kwargs)

    def telegram(self):
        return {"text": self.text_html, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": [
            [{"text": b.text, **({"callback_data": b.payload} if b.payload else {"url": b.url})} for b in row]
            for row in self.rows]}}

    def max_body(self, notify=True, reply_mid=None):
        buttons = []
        for row in self.rows:
            converted = []
            for b in row:
                if b.payload:
                    converted.append({"type": "callback", "text": b.text[:128], "payload": b.payload[:1024]})
                elif b.url and safe_url(b.url):
                    converted.append({"type": "link", "text": b.text[:128], "url": b.url[:2048]})
            buttons.extend(converted[n:n+3] for n in range(0, len(converted), 3))
        if self.item_id is not None:
            buttons.append([{"type": "callback", "text": "🏠 Меню", "payload": "menu:open"}])
        attachments = [{"type": "inline_keyboard", "payload": {"buttons": buttons[:30]}}] if buttons else []
        body = {"text": safe_html(self.text_html), "format": "html", "notify": notify, "attachments": attachments}
        if reply_mid:
            body["link"] = {"type": "reply", "mid": reply_mid}
        return body


def safe_url(url):
    try:
        p = urlsplit(url)
        return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False


class _HTML(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit, self.parts, self.stack = limit, [], []
        self.length = 0

    def add(self, text):
        reserve = sum(len(x) + 3 for x in self.stack)
        if self.length + len(text) + reserve <= self.limit:
            self.parts.append(text)
            self.length += len(text)
            return True
        return False

    def handle_starttag(self, tag, attrs):
        if tag not in ("b", "i", "a", "code", "blockquote", "pre"):
            return
        attrs = dict(attrs)
        if tag == "a" and not safe_url(attrs.get("href", "")):
            return
        text = f'<a href="{escape(attrs["href"], quote=True)}">' if tag == "a" else f"<{tag}>"
        self.stack.append(tag)
        if not self.add(text):
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.add(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, text):
        for ch in text:
            if not self.add(escape(ch, quote=False)):
                break


def safe_html(text, limit=4000):
    parser = _HTML(limit)
    parser.feed(text)
    parser.close()
    while parser.stack:
        parser.handle_endtag(parser.stack[-1])
    return "".join(parser.parts)


def card_for(ctx, iid, state=None, note="", links=False):
    item = ctx.db.item(iid)
    if not item:
        return Card("Публикация не найдена")
    from .privacy import protected_item
    item = protected_item(ctx, item)
    st, who, when = ctx.alerts.card_state(iid)
    state = state or st
    fb = ctx.db.one("SELECT note FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,))
    import re
    channel = re.search(r"\[channel:(Telegram|Max)\]", (fb["note"] or "") if fb else "")
    if channel:
        who = f"{who} · {channel[1]}"
    src = ctx.db.one("SELECT title FROM sources WHERE key=?", (item["source_key"],))
    risk = (jload(item.get("analysis_json"), {}) or {}).get("_risk", {})
    text, kb = _card_data(item, state, who, when, note, src["title"] if src else "",
                         foreign=ctx.alerts.foreign_button(item), check=bool(risk.get("hold")))
    al = ctx.db.one("SELECT escalations, created_at FROM alerts WHERE item_id=?", (iid,))
    if state == "open" and al and al["escalations"] and item["priority"] == "P1":
        mx = ctx.settings.alerts.max_escalations
        text += f"\n⏰ <b>Не взято в работу</b> · эскалация {min(al['escalations'], mx)}/{mx} · с {msk(al['created_at'], '%H:%M')}"
    copies = ctx.db.scalar("SELECT COUNT(*) FROM items WHERE id<>? AND json_extract(analysis_json, '$._risk.copy_of')=?", (iid, iid), 0)
    if copies:
        text += f"\n📎 Ещё {copies} копий этой истории — отдельно не присылаю (список: /skipped)"
    if state in ("false_positive", "resolved", "dropped"):
        from .cleanup import note_line
        gone = note_line(ctx, iid, state)
        if gone:
            text += f"\n<i>{gone}</i>"
    if state in ("open", "in_work", "escalated"):
        fu = ctx.alerts.followups(item)
        if fu:
            text += f"\n📰 <b>По теме после этого:</b> {fu['n']} публ. · последняя — {e(fu['source'])}, {msk(fu['at'])}"
    card = Card.from_tg(text[:4000], kb, item_id=iid)
    if links:
        row = []
        if safe_url(item.get("url") or ""):
            row.append(Btn("Открыть публикацию", url=item["url"]))
        if safe_url(ctx.settings.public_url):
            row.append(Btn("Карточка в Радаре", url=f"{ctx.settings.public_url.rstrip('/')}/item/{iid}"))
        if row:
            card.rows.append(row)
    return card
