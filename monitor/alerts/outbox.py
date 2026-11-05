"""Доставка: очередь исходящих (at-least-once), ретраи, фолбэк Telegram→почта→ретранслятор Colab, эскалации.

Почему очередь, а не «отправил и забыл»: Telegram в РФ с 01.04.2026 ограничен, SMTP бывает недоступен —
алерт не должен пропасть. Каждое сообщение лежит в outbox, пока не доставлено хотя бы одним каналом.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx

from ..util import ago, clip, html_escape as e, iso, jdump, jload, make_http, msk, now_utc, parse_iso
from .. import workhours
from . import subscriptions
from .emailer import send_email
from .formatting import PRIO_ICON, email_alert_html, objects_line, tg_alert, tg_card, tg_keyboard

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger("radar.alerts")


# ошибки «получателя», а не сети: ретранслятор тут не поможет
class TgRejected(RuntimeError):
    """Telegram принял запрос и отказал по содержанию (400): связь в порядке, повтор не поможет."""


def plain_text(text: str) -> str:
    """HTML-разметка → обычный текст (запасной вариант, если Telegram не разобрал теги)."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text or ""))


# ночная политика: что будит людей вне рабочего времени (остальные P2 ждут утра)
NIGHT_ADDR = {"gov_spb", "gov_lo", "gov_any", "prosecutor", "sk", "gzhi_spb", "gzhn_lo", "zhilkom_spb", "license",
              "media", "onf", "rpn"}
NIGHT_TOPICS = {"суд_прокуратура_надзор", "смена_УК_ОСС"}
from ..risk import MASS_RE, MASS_TOPICS  # noqa: E402  (одно определение для ночных правил и P1)

RECIPIENT_ERRORS = ("chat not found", "bot was blocked", "user is deactivated", "bot can't initiate",
                    "have no rights", "not enough rights", "bot was kicked")

class Alerts:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        from .max_delivery import MaxDelivery
        self.max = MaxDelivery(ctx)
        self._process_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._card_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self.tg_ok: bool | None = None       # последнее состояние доступности Telegram с сервера
        self.tg_ok_at: str | None = None     # когда последний раз сообщение дошло

    # ---------------------------------------------------------------- получатели (подписки — alerts/subscriptions.py)
    def recipients(self, event: str, channel: str) -> list[str]:
        return subscriptions.recipients(self.ctx, event, channel)

    def tg_recipients(self, level: int = 1) -> list[str]:
        """Все пользователи бота (для проверки доставки и ретранслятора). Кому что — через recipients()."""
        return sorted({r["address"] for r in subscriptions.rows(self.ctx) if r["channel"] == "tg"})

    def email_recipients(self) -> list[str]:
        return sorted({r["address"] for r in subscriptions.rows(self.ctx) if r["channel"] == "mail"})

    # ---------------------------------------------------------------- ночь: что будит, что ждёт утра
    def night_reason(self, item: dict) -> str | None:
        """Почему эту P2 нельзя отложить до утра (None — можно). P1 не откладываются никогда."""
        if item.get("priority") == "P1":
            return "критично"
        src = self.ctx.db.one("SELECT category FROM sources WHERE key=?", (item.get("source_key"),))
        if src and src["category"] in ("official", "governor", "media"):
            return "публикация СМИ или власти"
        m, a = jload(item.get("match_json"), {}) or {}, jload(item.get("analysis_json"), {}) or {}
        if set(m.get("addressees") or []) & NIGHT_ADDR:
            return "адресовано надзору, власти или СМИ"
        topics = set(a.get("topics") or []) | {t.split(":", 1)[1] for t in jload(item.get("tags"), []) or []
                                               if t.startswith("тема:")}
        if topics & NIGHT_TOPICS:
            return "надзор, прокуратура или смена УК"
        if topics & MASS_TOPICS and MASS_RE.search(f"{item.get('title') or ''} {item.get('text') or ''}"):
            return "массовая авария"
        if m.get("cta"):
            return "призыв к соседям (может разойтись за ночь)"
        return None

    # ---------------------------------------------------------------- постановка
    def enqueue(self, channel: str, recipient: str, payload: dict, dedup: str, item_id: int | None = None,
                priority: str = "", not_before: str | None = None) -> None:
        self.ctx.db.x("INSERT OR IGNORE INTO outbox(channel, recipient, payload_json, dedup_key, status, attempts, "
                      "next_attempt_at, created_at, item_id, priority) VALUES(?,?,?,?, 'pending', 0, ?, ?, ?, ?)",
                      (channel, recipient, jdump(payload), dedup, not_before or iso(), iso(), item_id, priority))

    def raise_alert(self, item_id: int, priority: str, update: bool = False, escalation: int = 0) -> None:
        db = self.ctx.db
        item = db.item(item_id)
        if not item:
            return
        from .privacy import protected_item
        from .cleanup import unmark
        item = protected_item(self.ctx, item)
        unmark(self.ctx, item_id)                   # снова срочно — карточку из чата не убираем
        if not escalation:
            db.x("INSERT INTO alerts(item_id, priority, created_at, last_notified_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(item_id) DO UPDATE SET priority=excluded.priority, last_notified_at=excluded.last_notified_at",
                 (item_id, priority, iso(), iso()))
        src = db.one("SELECT title FROM sources WHERE key=?", (item["source_key"],))
        stitle = src["title"] if src else ""
        text = tg_alert(item, update=update, escalation=escalation, source_title=stitle)
        tag = f"{priority}:{int(update)}:{escalation}"
        # ночью и в выходные P2 без надзора/СМИ/массовой аварии ждёт начала рабочего дня (охрана и диспетчер
        # разбираются сами; PR нужен утром). Если к утру закроют/понизят — не придёт вовсе.
        hold = None
        a_risk = ((jload(item["analysis_json"], {}) or {}).get("_risk") or {}) if "analysis_json" in item.keys() else {}
        if priority == "P2" and not escalation and self.ctx.settings.alerts.night_hold and workhours.is_off(self.ctx) \
                and (a_risk.get("hold") or not self.night_reason(dict(item))):
            # P2 «дом не из справочника — проверить» ждёт утра всегда: это вопрос к данным, а не авария (дизайн §4.3)
            hold = workhours.next_start(self.ctx)
            text = (f"🌙 <i>Пришло в нерабочее время ({msk(item['published_at'])}); не срочно ночью — отложено до "
                    f"{hold:%d.%m %H:%M}.</i>\n") + text
            db.event("info", "alerts", f"P2 #{item_id} отложен до {hold:%d.%m %H:%M} (нерабочее время)")
        event = "esc" if escalation else "p1" if priority == "P1" else "p2"
        chats = self.recipients(event, "tg")
        pinged = False
        for chat in dict.fromkeys(chats):
            if (escalation or update) and self.has_card(item_id, chat):
                # одна карточка на (публикация, чат): карточка правится на месте, плюс короткий пинг (дизайн §7)
                pinged |= self.ping(item_id, chat, priority, update, escalation)
                continue
            kb = tg_keyboard(item_id, check=bool(a_risk.get("hold")) and priority == "P2",
                             foreign=self.foreign_button(dict(item)))
            self.enqueue("telegram", chat, {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                            "disable_web_page_preview": True, "reply_markup": kb},
                         f"tg:{item_id}:{chat}:{tag}", item_id, priority, not_before=iso(hold) if hold else None)
        if not self.ctx.settings.max.only_when_tg_down or self.tg_ok is False or not self.ctx.settings.tg_bot_token:
            for chat in self.recipients(event, "max"):
                self.max.enqueue_card(item_id, chat, priority, hold, update, escalation)
        if self.max.enabled and self.ctx.settings.max.fallback_from_tg and self.ctx.settings.max_admin_chat_id and not hold \
                and (not chats or not self.ctx.settings.tg_bot_token):
            self.max.enqueue_card(item_id, self.ctx.settings.max_admin_chat_id, priority, update=update,
                                  escalation=escalation, fallback=True)
        if pinged:
            self.schedule_refresh(item_id)
        # письмо: при первом P1 и при первой эскалации (и ручной «🔥 Эскалировать»); повторы эскалации — только в Telegram,
        # иначе руководство получает по 4 письма на каждый не взятый вовремя P1
        if (priority == "P1" and not escalation and not update) or escalation in (1, 9) or \
                (update and priority == "P1" and not db.one("SELECT 1 FROM outbox WHERE channel='email' AND item_id=?", (item_id,))):
            subj, html = email_alert_html(item, stitle, self.ctx.settings.public_url)
            for mail in self.recipients(event, "mail"):
                self.enqueue("email", mail, {"to": [mail], "subject": subj, "html": html},
                             f"mail:{item_id}:{mail}:{tag}", item_id, priority)
        db.event("alert", "alerts", f"{priority} #{item_id} {'(повышение)' if update else ''}{'(эскалация)' if escalation else ''}")

    def has_card(self, item_id: int, chat: str) -> bool:
        db = self.ctx.db
        return bool(db.one("SELECT 1 FROM alert_msgs WHERE item_id=? AND chat_id=?", (item_id, str(chat))) or db.one(
            "SELECT 1 FROM outbox WHERE channel='telegram' AND recipient=? AND item_id=? AND dedup_key LIKE 'tg:%' "
            "AND status IN ('pending','relay','relay_leased','sent')", (str(chat), item_id)))

    def ping(self, item_id: int, chat: str, priority: str, update: bool, escalation: int) -> bool:
        """Короткое напоминание ответом на карточку; в чате максимум карточка + 1 пинг. Карточка ещё в очереди —
        пинг не нужен (придёт сама карточка)."""
        db = self.ctx.db
        msg = db.one("SELECT message_id FROM alert_msgs WHERE item_id=? AND chat_id=? ORDER BY sent_at DESC LIMIT 1",
                     (item_id, str(chat)))
        if not msg and db.one("SELECT 1 FROM outbox WHERE channel='telegram' AND recipient=? AND item_id=? "
                              "AND dedup_key LIKE 'tg:%' AND status='pending'", (str(chat), item_id)):
            return False
        db.x("UPDATE outbox SET status='dropped', last_error='заменён новым пингом' WHERE channel='telegram' "
             "AND recipient=? AND item_id=? AND status='pending' AND dedup_key LIKE 'ping:%'", (str(chat), item_id))
        mx = self.ctx.settings.alerts.max_escalations
        what = (f"⏰ не взято в работу (эскалация {escalation}/{mx})" if 0 < escalation < 9 else
                "🔥 эскалировано вручную" if escalation == 9 else f"⬆️ повышено до {priority}")
        payload = {"chat_id": chat, "text": f"{PRIO_ICON.get(priority, '')} <b>#{item_id}</b> · {what} — карточка выше",
                   "parse_mode": "HTML"}
        if msg:
            payload["reply_parameters"] = {"message_id": msg["message_id"], "allow_sending_without_reply": True}
        self.enqueue("telegram", chat, payload, f"ping:{item_id}:{chat}:{priority}:{int(update)}:{escalation}", item_id, priority)
        return True

    async def delete_pings(self, item_id: int, chat: str | None = None, keep: int | None = None) -> None:
        """Убрать отправленные пинги (best-effort, одна попытка): после «Беру/Решено/Ложное» и при новом пинге."""
        db = self.ctx.db
        rows = db.q("SELECT chat_id, message_id FROM alert_pings WHERE item_id=?" + (" AND chat_id=?" if chat else ""),
                    (item_id, str(chat)) if chat else (item_id,))
        for r in rows:
            if keep and r["message_id"] == keep:
                continue
            try:
                await self._tg("deleteMessage", {"chat_id": r["chat_id"], "message_id": r["message_id"]})
            except Exception as ex:  # noqa: BLE001 — сообщение могли удалить руками или оно старше 48 ч
                log.info("пинг #%s в %s не удалён: %s", item_id, r["chat_id"], ex)
            db.x("DELETE FROM alert_pings WHERE chat_id=? AND message_id=?", (r["chat_id"], r["message_id"]))

    async def delete_cards(self, item_id: int) -> tuple[int, bool]:
        """Убрать из Telegram карточку публикации и её пинги (уборка закрытых, alerts/cleanup.py).
        Возвращает (удалено, всё ли разобрано). «Нет такого сообщения», «нельзя удалить» (старше 48 ч, нет прав) —
        окончательный ответ: строку забываем, карточка остаётся свёрнутой. Сеть и 429 — повтор позже."""
        from .cleanup import tg_too_old

        db, n = self.ctx.db, 0
        if not self.ctx.settings.tg_bot_token:
            return 0, True
        rows = [("alert_msgs", r) for r in db.q("SELECT chat_id, message_id, sent_at FROM alert_msgs WHERE item_id=?", (item_id,))]
        rows += [("alert_pings", r) for r in db.q("SELECT chat_id, message_id, sent_at FROM alert_pings WHERE item_id=?", (item_id,))]
        for table, r in rows:
            if not tg_too_old(r["sent_at"]):
                try:
                    await self._tg("deleteMessage", {"chat_id": r["chat_id"], "message_id": r["message_id"]})
                    n += 1
                except TgRejected as ex:          # 400: уже удалено руками или старше 48 ч
                    log.info("карточка #%s в %s не удалена: %s", item_id, r["chat_id"], ex)
                except Exception as ex:  # noqa: BLE001
                    if "Telegram 403" not in str(ex):                       # 403: бота убрали из чата — не повторяем
                        log.warning("карточка #%s в %s: удаление отложено (%s)", item_id, r["chat_id"], type(ex).__name__)
                        return n, False
            db.x(f"DELETE FROM {table} WHERE chat_id=? AND message_id=?", (r["chat_id"], r["message_id"]))
        return n, True

    def drop_pings(self, item_id: int) -> None:
        try:
            asyncio.get_running_loop().create_task(self.delete_pings(item_id))
        except RuntimeError:
            pass

    def foreign_button(self, item: dict) -> dict | None:
        """«🚫 <здание> — не наш дом» на карточке, где разобран чужой корпус или другой дом не из справочника (§6)."""
        m = jload(item.get("match_json"), {}) or {}
        f = next((f for f in m.get("foreign") or [] if not f.get("known")), None)
        if f:
            return {"label": f["label"], "id": item["id"]}
        ev = next((h["evidence"] for h in m.get("hits", []) if h["kind"] == "street" and h["conf"] == 0.25), "")
        lab = ev.replace("улица: ", "").split(" — ")[0].strip()
        if lab and re.search(r"\d", lab):
            return {"label": lab, "id": item["id"]}
        return None

    def story_summary(self) -> int:
        """Пробная неделя (§5.5): одна утренняя сводка «склейки и чужие дома за сутки» вместо отдельных карточек."""
        db, cfg = self.ctx.db, self.ctx.settings.story
        start = db.kv_get("story_trial_from")
        if not start:
            db.kv_set("story_trial_from", iso())
            return 0
        if (now_utc() - parse_iso(start)).days >= cfg.trial_days or workhours.is_off(self.ctx):
            return 0
        day = msk(iso(), "%Y-%m-%d")
        if db.kv_get("story_summary_day") == day:
            return 0
        db.kv_set("story_summary_day", day)
        rows = [dict(r) for r in db.q("SELECT * FROM items WHERE analyzed_at>? AND (analysis_json LIKE '%\"cap_kind\": \"same_case\"%' "
                                      "OR analysis_json LIKE '%\"cap_kind\": \"foreign\"%') ORDER BY analyzed_at DESC",
                                      (ago(hours=24),))]
        if not rows:
            return 0
        lines = [f"🧩 <b>Склейки и чужие дома за сутки: {len(rows)}</b>",
                 "<i>Пробная неделя новой логики: эти публикации НЕ пришли отдельными алертами — копии отмеченной истории "
                 "или чужой корпус в нашем ЖК. Если что-то наше — «✅ Наше», вернётся алертом. Полный список — /skipped.</i>", ""]
        for it in rows[:10]:
            r = ((jload(it.get("analysis_json"), {}) or {}).get("_risk") or {})
            why = (r.get("combine") or "").split("не выше ")[-1][:160]
            lines.append(f"• <b>#{it['id']}</b> {e(objects_line(it))} — {e(clip(it.get('text') or '', 90))}\n  <i>{e(why)}</i> "
                         f"<a href=\"{e(it.get('url') or '')}\">открыть</a>")
        btns = [{"text": f"✅ Наше #{it['id']}", "callback_data": f"h:ours:{it['id']}"} for it in rows[:8]]
        kb = {"inline_keyboard": [btns[i:i + 2] for i in range(0, len(btns), 2)]}
        for chat in dict.fromkeys(self.recipients("story", "tg")):
            self.enqueue("telegram", chat, {"chat_id": chat, "text": "\n".join(lines)[:4000], "parse_mode": "HTML",
                                            "disable_web_page_preview": True, "reply_markup": kb}, f"story:{day}:{chat}")
        from .render import Card
        for chat in self.recipients("story", "max"):
            self.max.enqueue_text(chat, Card.from_tg("\n".join(lines)[:4000], kb), f"maxstory:{day}:{chat}")
        return len(rows)

    # ---------------------------------------------------------------- «найдено с опозданием» — одной сводкой
    def to_digest(self, item_id: int, priority: str) -> None:
        """Инцидент заводим (он будет в отчётах и на дашборде), но без отдельного сообщения и без эскалаций."""
        db = self.ctx.db
        db.x("INSERT INTO alerts(item_id, priority, created_at, last_notified_at, escalations) VALUES(?,?,?,?,?) "
             "ON CONFLICT(item_id) DO UPDATE SET priority=excluded.priority",
             (item_id, priority, iso(), iso(), self.ctx.settings.alerts.max_escalations))
        pend = [p for p in (db.kv_get("digest_pending", []) or []) if p["id"] != item_id]
        pend.append({"id": item_id, "prio": priority, "at": iso()})
        db.kv_set("digest_pending", pend[-200:])
        db.event("info", "alerts", f"{priority} #{item_id} найден с опозданием — в сводку «найдено с опозданием»")

    def flush_digest(self, settle_min: float = 2, max_wait_min: float = 10) -> int:
        """Раз в несколько минут: всё, что накопилось, — одним сообщением. Ждём, пока «волна» первого сбора
        уляжется (2 мин без новых находок), но не дольше 10 минут."""
        db = self.ctx.db
        pend = db.kv_get("digest_pending", []) or []
        if not pend:
            return 0
        newest, oldest = max(p["at"] for p in pend), min(p["at"] for p in pend)
        if newest > ago(minutes=settle_min) and oldest > ago(minutes=max_wait_min):
            return 0
        db.kv_set("digest_pending", [])
        items = []
        for p in pend:
            it = db.item(p["id"])
            if it and it["priority"] in self.ctx.settings.alerts.instant_priorities and it["workflow"] in ("open", "in_work"):
                items.append(dict(it))
        if not items:
            return 0
        items.sort(key=lambda x: x["published_at"] or "", reverse=True)     # внутри приоритета — свежие выше
        items.sort(key=lambda x: x["priority"])
        has_p1 = any(x["priority"] == "P1" for x in items)
        n1 = sum(x["priority"] == "P1" for x in items)
        stamp = now_utc().strftime("%Y%m%d%H%M%S")
        lines = [f"📥 <b>Найдено с опозданием: {len(items)}</b>" + (f" (🔴 P1: {n1})" if n1 else ""),
                 "<i>Опубликовано раньше, чем Радар это увидел: подключили новый источник или человека, либо был перерыв "
                 "в работе. Отдельными алертами не присылаю, эскалаций по ним не будет. Кнопка открывает полную карточку.</i>", ""]
        for it in items[:12]:
            a = jload(it.get("analysis_json"), {}) or {}
            gist = e(clip(a.get("summary") or it.get("text") or "", 170))
            lines.append(f"{PRIO_ICON.get(it['priority'], '⚪')} <b>#{it['id']} · {e(objects_line(it))}</b> — "
                         f"{e(it.get('author_name') or '—')}, {msk(it['published_at'])}\n{gist} "
                         f"<a href=\"{e(it.get('url') or '')}\">открыть</a>\n")
        if len(items) > 12:
            lines.append(f"…и ещё {len(items) - 12} — в Радаре на дашборде")
        text = "\n".join(lines)[:4000]
        btns = [{"text": f"{PRIO_ICON.get(it['priority'], '⚪')} #{it['id']}", "callback_data": f"a:card:{it['id']}"}
                for it in items[:8]]
        kb = {"inline_keyboard": [btns[i:i + 4] for i in range(0, len(btns), 4)]}
        hold = None
        if not has_p1 and self.ctx.settings.alerts.night_hold and workhours.is_off(self.ctx):
            hold = workhours.next_start(self.ctx)
            text = f"🌙 <i>Отложено до {hold:%d.%m %H:%M} (нерабочее время).</i>\n" + text
        chats = self.recipients("digest", "tg")
        for chat in dict.fromkeys(chats):
            self.enqueue("telegram", chat, {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                            "disable_web_page_preview": True, "reply_markup": kb},
                         f"digest:{stamp}:{chat}", None, "P1" if has_p1 else "P2", not_before=iso(hold) if hold else None)
        from .render import Card
        for chat in self.recipients("digest", "max"):
            self.max.enqueue_text(chat, Card.from_tg(text, kb), f"maxdigest:{stamp}:{chat}", hold=hold,
                                  priority="P1" if has_p1 else "P2")
        if has_p1:
            rows = "".join(f"<li><b>{x['priority']}</b> · {e(objects_line(x))} — {e(x.get('author_name') or '—')}, "
                           f"{msk(x['published_at'])}: {e(clip((jload(x.get('analysis_json'), {}) or {}).get('summary') or x.get('text') or '', 220))} "
                           f"<a href=\"{e(x.get('url') or '')}\">открыть</a></li>" for x in items[:20])
            html = (f"<div style='font-family:Arial,sans-serif;max-width:720px'><h3>Найдено с опозданием: {len(items)}</h3>"
                    f"<p style='color:#555'>Опубликовано раньше, чем Радар это увидел (новый источник или перерыв в работе).</p>"
                    f"<ul>{rows}</ul></div>")
            for mail in self.recipients("p1", "mail"):
                self.enqueue("email", mail, {"to": [mail], "subject": f"Радар: найдено с опозданием — {len(items)} (P1: {n1})",
                                             "html": html}, f"digestmail:{stamp}:{mail}", None, "P1")
        db.event("alert", "alerts", f"сводка «найдено с опозданием»: {len(items)} (P1: {n1})")
        return len(items)

    def admin_notice(self, text: str, dedup: str, include_max: bool = True) -> None:
        """Служебные сообщения (Colab отключился, источники упали, токен протух)."""
        for chat in self.recipients("service", "tg"):
            self.enqueue("telegram", chat, {"chat_id": chat, "text": text, "parse_mode": "HTML"}, f"adm:{dedup}:{chat}")
        for mail in self.recipients("service", "mail"):
            self.enqueue("email", mail, {"to": [mail], "subject": "Радар: служебное уведомление", "html": f"<p>{text}</p>"},
                         f"admmail:{dedup}:{mail}")

        if include_max:
            from .render import Card
            for chat in self.recipients("service", "max"):
                self.max.enqueue_text(chat, Card(text), f"maxadm:{dedup}:{chat}")

    def ack(self, item_id: int, user: str, workflow: str = "in_work") -> None:
        self.ctx.db.x("UPDATE alerts SET acked_by=COALESCE(acked_by, ?), acked_at=COALESCE(acked_at, ?) WHERE item_id=?",
                      (user, iso(), item_id))
        self.ctx.memory.feedback(item_id, workflow, user)
        if workflow == "false_positive":
            self.ctx.db.x("UPDATE items SET priority='P4' WHERE id=?", (item_id,))
        self.drop_pings(item_id)
        self.queue_refresh(item_id)

    # ---------------------------------------------------------------- отправка
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = make_http(proxy=self.ctx.settings.network.telegram_proxy, timeout=20)
        return self._http

    async def send_tg(self, payload: dict) -> dict:
        token = self.ctx.settings.tg_bot_token
        if not token:
            raise RuntimeError("TG_BOT_TOKEN не задан")
        return await self._tg("sendMessage", payload)

    async def _tg(self, method: str, payload: dict) -> dict:
        token = self.ctx.settings.tg_bot_token
        if len(payload.get("text") or "") > 4000:              # лимит Telegram — 4096 символов
            payload = {**payload, "text": payload["text"][:3990].rsplit("\n", 1)[0] + "\n…"}
        r = await self.http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429:
            await asyncio.sleep(int((j.get("parameters") or {}).get("retry_after", 3)))
            raise RuntimeError("Telegram 429")
        desc = (j.get("description") or "").lower()
        if r.status_code == 400 and "text" in payload and ("parse" in desc or "entit" in desc or "tag" in desc):
            # в тексте попался «<» или «&» — шлём без разметки (ключ parse_mode убираем: null Telegram не принимает)
            log.info("Telegram не разобрал HTML (%s) — отправляю простым текстом", j.get("description"))
            payload = {k: v for k, v in payload.items() if k != "parse_mode"}
            payload["text"] = plain_text(payload["text"])
            r = await self.http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not j.get("ok"):
            err = TgRejected if r.status_code == 400 else RuntimeError
            raise err(f"Telegram {r.status_code}: {j.get('description')}")
        return j.get("result") or {}

    # ---------------------------------------------------------------- карточки: одна правка — у всех получателей
    def card_state(self, item_id: int) -> tuple[str, str, str]:
        """(состояние, кто, когда) — из статуса публикации и журнала решений."""
        db = self.ctx.db
        it = db.item(item_id) or {}
        wf = it.get("workflow") or "open"
        fb = db.one("SELECT user, ts, verdict FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,))
        who, when = (fb["user"], msk(fb["ts"], "%d.%m %H:%M")) if fb else ("", "")
        if wf in ("false_positive", "resolved", "in_work"):
            return wf, who, when
        al = db.one("SELECT escalations FROM alerts WHERE item_id=?", (item_id,))
        if fb and fb["verdict"] == "escalated":
            return "escalated", who, when
        return "open", "", ""

    async def refresh_cards(self, item_id: int, state: str | None = None, note: str = "") -> int:
        async with self._card_lock:
            # Operator status always comes from the latest committed decision, never a stale task.
            return await self._refresh_cards(item_id, state if state == "dropped" else None, note)

    async def _refresh_cards(self, item_id: int, state: str | None = None, note: str = "") -> int:
        """Переписывает все отправленные копии карточки под текущее состояние. Возвращает число правок."""
        db = self.ctx.db
        item = db.item(item_id)
        if not item:
            return 0
        from .render import card_for
        st = self.card_state(item_id)[0]
        state = state or st
        card = card_for(self.ctx, item_id, state, note)
        text, kb = card.text_html, card.telegram()["reply_markup"]
        n = 0
        failed = False
        for m in db.q("SELECT chat_id, message_id FROM alert_msgs WHERE item_id=?", (item_id,)):
            try:
                await self._tg("editMessageText", {"chat_id": m["chat_id"], "message_id": m["message_id"], "text": text,
                                                   "parse_mode": "HTML", "disable_web_page_preview": True,
                                                   "reply_markup": kb})
                n += 1
            except Exception as ex:
                if "not modified" in str(ex):
                    n += 1
                elif "not found" in str(ex) or "can't be edited" in str(ex):
                    db.x("DELETE FROM alert_msgs WHERE chat_id=? AND message_id=?", (m["chat_id"], m["message_id"]))
                else:
                    failed = True
                    log.warning("карточка #%s в %s не обновлена: %s", item_id, m["chat_id"], ex)
        try:
            n += await self.max.refresh(item_id, state, note)
        except Exception as ex:
            failed = True
            log.warning("Max: карточка #%s ожидает повтора (%s)", item_id, type(ex).__name__)
        if failed:
            self.queue_refresh(item_id, state, note, delay=15)
        return n

    def queue_refresh(self, item_id, state=None, note="", delay=0):
        if state == "dropped":                      # «⚪ Снято автоматически» — тоже уйдёт из чата (alerts/cleanup.py)
            from .cleanup import mark_dropped
            mark_dropped(self.ctx, item_id)
        self.ctx.db.x("INSERT INTO card_refresh(item_id,state,note,next_attempt_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(item_id) DO UPDATE SET state=excluded.state,note=excluded.note,"
                      "revision=card_refresh.revision+1,next_attempt_at=excluded.next_attempt_at",
                      (item_id, state, note, iso(now_utc()+timedelta(seconds=delay))))

    async def flush_refresh(self):
        async with self._refresh_lock:
            for row in self.ctx.db.q("SELECT * FROM card_refresh WHERE next_attempt_at<=? LIMIT 20", (iso(),)):
                await self.refresh_cards(row["item_id"], row["state"], row["note"])
                self.ctx.db.x("DELETE FROM card_refresh WHERE item_id=? AND revision=?", (row["item_id"], row["revision"]))

    def followups(self, item: dict) -> dict | None:
        """Сколько публикаций вышло по той же теме в том же районе после этого алерта (волна)."""
        from ..pipeline import Pipeline

        tags = jload(item.get("tags"), []) or []
        places, topics = Pipeline.story_sig(tags)
        if not (places and topics) or not item.get("published_at"):
            return None
        rows = [r for r in self.ctx.db.q(
            "SELECT tags, source_key, published_at FROM items WHERE published_at>? AND published_at<? AND id<>? "
            "AND priority IN ('P1','P2','P3') ORDER BY published_at", (item["published_at"], iso(parse_iso(item["published_at"])
                                                                             + timedelta(hours=24)), item["id"]))
                if Pipeline.same_story(tags, jload(r["tags"], []) or [])]
        if not rows:
            return None
        src = self.ctx.db.one("SELECT title FROM sources WHERE key=?", (rows[-1]["source_key"],))
        return {"n": len(rows), "source": src["title"] if src else rows[-1]["source_key"], "at": rows[-1]["published_at"]}

    def schedule_refresh(self, item_id: int, state: str | None = None, note: str = "") -> None:
        """Из синхронного кода (конвейер): поправить карточки, когда будет возможность."""
        self.queue_refresh(item_id, state, note)
        try:
            asyncio.get_running_loop().create_task(self.flush_refresh())
        except RuntimeError:   # нет цикла (разовый запуск/тесты) — карточки поправятся при следующем действии
            pass

    def reopen(self, item_id: int, user: str) -> None:
        """«↩️ Вернуть»: снова открыто, прежний приоритет, ошибочная пометка «ложное» убрана из памяти агента."""
        db = self.ctx.db
        al = db.one("SELECT priority FROM alerts WHERE item_id=?", (item_id,))
        db.x("DELETE FROM feedback WHERE item_id=? AND verdict IN ('false_positive','resolved')", (item_id,))
        db.x("UPDATE items SET workflow='open', priority=COALESCE(?, priority) WHERE id=?",
             (al["priority"] if al else None, item_id))
        db.x("UPDATE alerts SET acked_by=NULL, acked_at=NULL, last_notified_at=? WHERE item_id=?", (iso(), item_id))
        self.ctx.memory.feedback(item_id, "reopened", user)      # явное «это наше» — белый корень истории (дизайн §5.2)
        from .cleanup import unmark
        unmark(self.ctx, item_id)
        db.event("info", "alerts", f"#{item_id} возвращено в работу ({user})")
        try:
            self.ctx.pipeline.reconcile_up("↩️ Вернуть")
        except Exception:  # noqa: BLE001
            log.exception("пересмотр после «Вернуть» не удался")
        # карточку уже убрали из чатов (уборка закрытых) — вернуть срочное в чаты заново, иначе его никто не увидит
        it = db.item(item_id) or {}
        if it.get("priority") in self.ctx.settings.alerts.instant_priorities and not db.one(
                "SELECT 1 FROM alert_msgs WHERE item_id=?", (item_id,)) and not db.one(
                "SELECT 1 FROM max_msgs WHERE item_id=? AND kind='card'", (item_id,)) and not db.one(
                "SELECT 1 FROM outbox WHERE item_id=? AND status IN ('pending','relay','relay_leased') AND "
                "(dedup_key LIKE 'tg:%' OR dedup_key LIKE 'max:%' OR dedup_key LIKE 'maxmanual:%')", (item_id,)):
            db.x("DELETE FROM outbox WHERE item_id=? AND (dedup_key LIKE 'tg:%' OR dedup_key LIKE 'max:%')", (item_id,))
            self.raise_alert(item_id, it["priority"])

    def still_relevant(self, row) -> tuple[bool, str]:
        """Алерт мог устареть, пока лежал в очереди: ложное/решено/понижено — не отправляем."""
        dk = str(row["dedup_key"] or "")
        if not row["item_id"] or not dk.startswith(("tg:", "mail:", "fallback:", "ping:", "max:", "maxping:")):
            return True, ""
        it = self.ctx.db.item(row["item_id"])
        if not it:
            return False, "публикация удалена"
        if it["workflow"] in ("false_positive", "resolved"):
            return False, f"уже отмечено: {it['workflow']}"
        if dk.startswith(("max:", "maxping:")) and (((jload(it.get("analysis_json"), {}) or {}).get("_risk") or {}).get("copy_of")):
            return False, "копия истории — карточка только у корня"
        if dk.startswith(("ping:", "maxping:")):
            al = self.ctx.db.one("SELECT acked_at FROM alerts WHERE item_id=?", (row["item_id"],))
            if it["workflow"] != "open" or (al and al["acked_at"]) or it["priority"] not in self.ctx.settings.alerts.instant_priorities:
                return False, "пинг устарел: взято в работу или понижено"
            return True, ""
        if it["priority"] not in self.ctx.settings.alerts.instant_priorities and ":9" not in dk:
            return False, f"приоритет понижен до {it['priority']}"
        from ..risk import ORDER

        hold = (((jload(it.get("analysis_json"), {}) or {}).get("_risk") or {}).get("hold"))
        if hold and row["priority"] and ORDER.get(row["priority"], 4) < ORDER.get(it["priority"], 4) and ":9" not in dk:
            return False, f"приоритет понижен до {it['priority']} (дом не из справочника — придёт утром)"
        return True, ""

    async def process(self, limit: int = 30) -> int:
        # Max delivery starts independently of slow Telegram/SMTP calls.
        async with self._process_lock:
            results = await asyncio.gather(self._process_other(limit), self.max.process(limit), return_exceptions=True)
            sent = sum(x for x in results if isinstance(x, int))
            for result in results:
                if isinstance(result, Exception):
                    log.error("доставка: %s", type(result).__name__)
            sent += await self.max.process(limit)
            await self.flush_refresh()
            return sent

    async def _process_other(self, limit: int = 30) -> int:
        db, sent = self.ctx.db, 0
        rows = db.q("SELECT * FROM outbox WHERE channel<>'max' AND status='pending' AND next_attempt_at<=? ORDER BY priority='P1' DESC, id "
                    "LIMIT ?", (iso(), limit))
        relay_after = self.ctx.settings.alerts.telegram_fail_to_relay_after
        for r in rows:
            payload = jload(r["payload_json"], {})
            ok, why = self.still_relevant(r)
            if not ok:
                db.x("UPDATE outbox SET status='dropped', last_error=? WHERE id=?", (why, r["id"]))
                log.info("outbox #%s не отправлен: %s", r["id"], why)
                continue
            try:
                if r["channel"] == "telegram":
                    res = await self.send_tg(payload) or {}
                    self.tg_ok = True
                    self.tg_ok_at = iso()
                    if r["item_id"] and payload.get("reply_markup") and res.get("message_id"):
                        db.x("INSERT OR IGNORE INTO alert_msgs(item_id, chat_id, message_id, sent_at) VALUES(?,?,?,?)",
                             (r["item_id"], str(payload["chat_id"]), res["message_id"], iso()))
                        if str(r["dedup_key"] or "").startswith("tg:") and (r["created_at"] or "") < ago(minutes=2):
                            self.schedule_refresh(r["item_id"])   # пролежала в очереди: дорисовать текущее состояние
                    if r["item_id"] and str(r["dedup_key"] or "").startswith("ping:") and res.get("message_id"):
                        await self.delete_pings(r["item_id"], str(payload["chat_id"]), keep=res["message_id"])
                        db.x("INSERT OR IGNORE INTO alert_pings(item_id, chat_id, message_id, sent_at) VALUES(?,?,?,?)",
                             (r["item_id"], str(payload["chat_id"]), res["message_id"], iso()))
                elif r["channel"] == "email":
                    await send_email(self.ctx.settings.smtp, payload["to"], payload["subject"], payload["html"],
                                     payload.get("attachments"))
                else:
                    raise ValueError("неизвестный канал доставки")
                db.x("UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?", (iso(), r["id"]))
                sent += 1
            except Exception as e:
                if r["channel"] == "telegram":
                    self.max.fallback(r)
                attempts = r["attempts"] + 1
                delay = min(600, 15 * 2 ** min(attempts, 6))
                status = "pending"
                if r["channel"] == "telegram" and any(x in str(e).lower() for x in RECIPIENT_ERRORS):
                    if attempts == 1:
                        self._email_fallback(r)
                        db.event("warning", "alerts", f"Telegram {r['recipient']}: {e} — получатель должен открыть "
                                                      f"бота и нажать «Старт»; пока алерты дублируются почтой")
                        u = db.one("SELECT name, email FROM users WHERE tg_id=?", (str(r["recipient"]),))
                        if r["dedup_key"] and not str(r["dedup_key"]).startswith("adm:"):
                            who = html_lib.escape(str(u["name"] if u and u["name"] else r["recipient"]))
                            self.admin_notice(f"📵 {who} не получает алерты в Telegram: не открыл бота или не нажал «Старт». "
                                              + ("Пока копии идут ему на почту." if u and u["email"] else
                                                 "Почта у него не указана — алерты до него не доходят."),
                                              f"nostart:{r['recipient']}:{now_utc():%Y%m%d}")
                    db.x("UPDATE outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                         (attempts, str(e)[:300], iso(now_utc() + timedelta(minutes=15)), r["id"]))
                    log.warning("outbox #%s: %s → получатель %s не нажал «Старт» у бота (повтор через 15 мин)",
                                r["id"], e, r["recipient"])
                    continue
                if r["channel"] == "telegram" and isinstance(e, TgRejected):
                    # отказ по содержанию: связь с Telegram в порядке, повторять бессмысленно — алерт дублируем письмом
                    self._email_fallback(r)
                    db.x("UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?", (attempts, str(e)[:300], r["id"]))
                    db.event("warning", "alerts", f"сообщение #{r['id']} Telegram отклонил: {e}")
                    log.warning("outbox #%s отклонён Telegram (%s) — не повторяю%s", r["id"], e,
                                ", продублирован почтой" if r["item_id"] else "")
                    continue
                if r["channel"] == "telegram":
                    self.tg_ok = False
                    if attempts >= relay_after:
                        status = "relay"    # пусть доставит Colab (он за рубежом, Telegram там не ограничен)
                        self._email_fallback(r)
                elif attempts >= 12:
                    status = "dead"
                db.x("UPDATE outbox SET status=?, attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                     (status, attempts, str(e)[:300], iso(now_utc() + timedelta(seconds=delay)), r["id"]))
                log.warning("outbox #%s %s → %s: %s", r["id"], r["channel"], status, e)
        # возврат «зависших» у ретранслятора (Colab взял и пропал)
        db.x("UPDATE outbox SET status='relay', relay_owner=NULL WHERE status='relay_leased' AND relay_until<?", (iso(),))
        return sent

    def recipient_ready(self, chat_id: str) -> int:
        """Человек нажал «Старт» — всё, что ему копилось (в т.ч. отданное ретранслятору), отправляем сейчас."""
        return self.ctx.db.x("UPDATE outbox SET status='pending', next_attempt_at=? WHERE channel='telegram' "
                             "AND recipient=? AND status IN ('pending','relay')", (iso(), str(chat_id)))

    def _email_fallback(self, row) -> None:
        """Telegram не дошёл до конкретного человека — отправляем алерт на ЕГО почту (из таблицы пользователей),
        если он не получает этот алерт письмом и так. Раньше копия уходила всем подписчикам почты: один сотрудник,
        не нажавший «Старт» у бота, удваивал каждое письмо у всех (30.09)."""
        if not row["item_id"]:
            return
        db = self.ctx.db
        item = db.item(row["item_id"])
        if not item:
            return
        u = db.one("SELECT email FROM users WHERE tg_id=? AND active=1", (str(row["recipient"]),))
        mail = ((u["email"] if u else "") or "").strip().lower()
        if "@" not in mail:
            return
        if db.one("SELECT 1 FROM outbox WHERE channel='email' AND item_id=? AND recipient=? AND dedup_key NOT LIKE 'fallback:%'",
                  (row["item_id"], mail)):
            return                                           # это письмо у человека уже есть
        subj, html = email_alert_html(item, "", self.ctx.settings.public_url)
        self.enqueue("email", mail, {"to": [mail], "subject": "[Telegram не доставлен] " + subj, "html": html},
                     f"fallback:{row['item_id']}:{mail}:{item['priority']}", row["item_id"], item["priority"])

    # ---------------------------------------------------------------- ретранслятор (Colab)
    def relay_lease(self, owner: str, limit: int = 20) -> list[dict]:
        db = self.ctx.db
        rows = db.q("SELECT id, payload_json FROM outbox WHERE status='relay' ORDER BY id LIMIT ?", (limit,))
        for r in rows:
            db.x("UPDATE outbox SET status='relay_leased', relay_owner=?, relay_until=? WHERE id=?",
                 (owner, iso(now_utc() + timedelta(minutes=3)), r["id"]))
        return [{"id": r["id"], "payload": jload(r["payload_json"], {})} for r in rows]

    def relay_ack(self, ids_ok: list[int], ids_fail: list[int]) -> None:
        for i in ids_ok:
            self.ctx.db.x("UPDATE outbox SET status='sent', sent_at=?, last_error='via colab relay' WHERE id=?", (iso(), i))
        for i in ids_fail:
            self.ctx.db.x("UPDATE outbox SET status='relay' WHERE id=?", (i,))

    # ---------------------------------------------------------------- эскалации P1
    def escalate(self) -> int:
        cfg, db, n = self.ctx.settings.alerts, self.ctx.db, 0
        rows = db.q("SELECT a.*, i.priority p FROM alerts a JOIN items i ON i.id=a.item_id WHERE a.acked_at IS NULL "
                    "AND i.priority='P1' AND i.workflow='open' AND a.escalations<? AND a.last_notified_at<?",
                    (cfg.max_escalations, ago(minutes=cfg.escalate_after_min)))
        for r in rows:
            esc = r["escalations"] + 1
            db.x("UPDATE alerts SET escalations=?, last_notified_at=? WHERE id=?", (esc, iso(), r["id"]))
            self.raise_alert(r["item_id"], "P1", escalation=esc)
            n += 1
        return n
