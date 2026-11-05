"""MAX screens, actor-bound navigation and durable background report requests."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

from ..util import ago, clip, html_escape as e, iso, jdump, jload
from . import commands, subscriptions
from .render import Btn, Card

log = logging.getLogger("radar.max")
PAGE = 5
ENTRY_COMMANDS = {"menu", "status", "brain", "hot", "feed", "find", "item", "skipped", "scan", "report",
                  "sources", "cand", "settings"}
OPTIONS = {"report_files": "Excel и Word к отчётам", "pin_p1": "Закрепление P1",
           "only_when_tg_down": "Карточки только при сбое Telegram", "fallback_from_tg": "Резерв при сбое Telegram"}
EVENT_LABELS = {**subscriptions.EVENTS, "rep_day": "📊 Дневной и вечерний", "story": "🧩 Повторы и отсев"}


@dataclass
class ScreenResult:
    card: Card | None = None
    notification: str = ""
    item_id: int | None = None


class MaxMenu:
    def __init__(self, bot):
        self.bot = bot
        self.ctx = bot.ctx
        self._worker = None

    def scope(self, actor, destination):
        return hashlib.sha256((actor.user_id + ":" + destination).encode()).hexdigest()[:24]

    def save(self, token, session):
        self.ctx.db.kv_set("max_menu:" + token, session)

    def session(self, token):
        return self.ctx.db.kv_get("max_menu:" + token, {})

    def button(self, token, label, action):
        return Btn(label, f"m:{token}:{action}")

    def navigation(self, token, back="home"):
        return [self.button(token, "⬅️ Назад", back), self.button(token, "🏠 Меню", "home")]

    def card(self, token, text, rows=None, back="home"):
        return Card(text, [*(rows or []), self.navigation(token, back)])

    def ref(self, session, value):
        key = hashlib.sha256(value.encode()).hexdigest()[:16]
        session.setdefault("refs", {})[key] = value
        return key

    def admin(self, actor, session):
        return actor.role == "admin" and session["private"]

    def operator(self, actor):
        return actor.role in ("manager", "admin")

    def clear_input(self, actor, destination):
        self.ctx.db.x("DELETE FROM kv WHERE key=?", ("max_input:" + self.scope(actor, destination),))

    def prompt(self, token, session, actor, kind, text, back="home"):
        self.ctx.db.kv_set("max_input:" + self.scope(actor, session["destination"]),
                           {"token": token, "kind": kind, "expires": time.time() + 600})
        return self.card(token, text + "\nОжидаю ответ 10 минут. «Назад» отменит ввод.", back=back)

    async def open(self, actor, destination, private, name="menu", args=""):
        self.clear_input(actor, destination)
        token = secrets.token_hex(6)
        session = {"owner": actor.user_id, "destination": destination, "private": private,
                   "expires": time.time() + 86400, "refs": {}}
        self.save(token, session)
        action = "home" if name in ("menu", "start") else name
        if name in ("report", "scan") and args:
            action += ":" + args
        elif name == "item" and args.isdigit():
            return token, ScreenResult(self.home(token, actor, session), item_id=int(args))
        elif name == "find" and args:
            session["query"] = args[:500]
            action = "search:0"
        result = await self.execute(token, session, actor, action, "", int(time.time()*1000))
        self.save(token, session)
        return token, result

    def home(self, token, actor, session):
        rows = [[self.button(token, "📊 Статус", "status"), self.button(token, "🔥 Горячее", "hot:0")],
                [self.button(token, "📋 Лента", "feed:0"), self.button(token, "🔎 Поиск", "find")],
                [self.button(token, "🧠 ИИ", "brain"), self.button(token, "🧩 Повторы и отсев", "skipped")]]
        if self.operator(actor):
            rows.append([self.button(token, "📊 Отчёт", "report"), self.button(token, "📡 Сбор сейчас", "scan")])
        rows.append([self.button(token, "📡 Источники", "sources:0"), self.button(token, "🆕 Кандидаты", "cand:0")])
        if self.admin(actor, session):
            rows.append([self.button(token, "⚙️ Настройки", "settings"), self.button(token, "👤 Наблюдение", "watch")])
            rows.append([self.button(token, "👥 Пользователи", "users")])
        rows.append([self.button(token, "🔢 Открыть карточку", "item"), self.button(token, "❓ Помощь", "help")])
        text = f"<b>Меню Радара</b>\n{e(self.ctx.settings.company)}\nВыберите действие."
        if not session["private"]:
            text += "\nНастройки и управление источниками доступны администратору в личке."
        return Card(text, rows)

    def page_buttons(self, token, prefix, offset, total):
        rows = []
        nav = []
        if offset:
            nav.append(self.button(token, "◀️ Предыдущие", f"{prefix}:{max(0, offset-PAGE)}"))
        if offset + PAGE < total:
            nav.append(self.button(token, "Следующие ▶️", f"{prefix}:{offset+PAGE}"))
        if nav:
            rows.append(nav)
        return rows

    def items(self, token, session, kind, offset):
        db = self.ctx.db
        if kind == "search":
            from ..textnorm import lemma_string
            items = db.fts_search(lemma_string(session.get("query", "")), 100)
        else:
            where = "analyzed_at>=? AND priority IN ('P1','P2')" if kind == "hot" else "status='analyzed' AND relevant=1"
            items = db.q("SELECT * FROM items WHERE " + where + " ORDER BY analyzed_at DESC, id DESC LIMIT 100",
                         (ago(hours=24),) if kind == "hot" else ())
        offset = min(offset, max(0, ((len(items)-1)//PAGE)*PAGE))
        title = {"hot": "🔥 Горячее за 24 часа", "feed": "📋 Последние публикации", "search": "🔎 Результаты поиска"}[kind]
        page = items[offset:offset+PAGE]
        lines = [f"<b>{title}</b>"]
        if kind == "search":
            lines.append("Запрос: " + e(session.get("query", "")))
        rows = []
        for it in page:
            lines.append(commands.item_short(self.ctx, it) if it["priority"] else f"#{it['id']} {e(clip(it['text'], 200))}")
            rows.append([self.button(token, f"Открыть #{it['id']}", f"item:{it['id']}")])
        if not page:
            lines.append("За сутки P1/P2 нет ✅" if kind == "hot" else "Публикаций не найдено.")
        else:
            lines.append(f"{offset+1}–{offset+len(page)} из {len(items)}")
        rows += self.page_buttons(token, kind, offset, len(items))
        session["return"] = f"{kind}:{offset}"
        return self.card(token, "\n\n".join(lines), rows)

    def sources(self, token, session, actor, offset, bad=False):
        rows = self.ctx.db.q("SELECT * FROM sources WHERE candidate=0 " +
                             ("AND enabled=1 AND consecutive_failures>=3 " if bad else "") +
                             "ORDER BY enabled DESC, tier, kind, key LIMIT 500")
        offset = min(offset, max(0, ((len(rows)-1)//PAGE)*PAGE))
        page = rows[offset:offset+PAGE]
        lines = ["<b>📡 Источники" + (" с ошибками" if bad else "") + "</b>"]
        buttons = []
        for i, src in enumerate(page, offset+1):
            mark = "⏸" if not src["enabled"] else "🔴" if (src["consecutive_failures"] or 0) >= 3 else "🟢"
            lines.append(f"{i}. {mark} {e(clip(src['title'] or src['key'], 100))}\n<code>{e(src['key'])}</code>")
            ref = self.ref(session, src["key"])
            buttons.append([self.button(token, f"Источник {i}", f"source:{ref}")])
        if not page:
            lines.append("Источников с ошибками нет." if bad else "Источников нет.")
        buttons += self.page_buttons(token, "bad" if bad else "sources", offset, len(rows))
        buttons.append([self.button(token, "Все источники" if bad else "С ошибками", "sources:0" if bad else "bad:0")])
        if self.admin(actor, session):
            buttons.append([self.button(token, "➕ Добавить источник", "source_add")])
        return self.card(token, "\n\n".join(lines), buttons)

    def candidates(self, token, session, actor, offset):
        from .. import candidates
        items = candidates.listing(self.ctx)
        offset = min(offset, max(0, ((len(items)-1)//PAGE)*PAGE))
        page = items[offset:offset+PAGE]
        lines, rows = ["<b>🆕 Кандидаты в источники</b>\nПаблики, где найдено упоминание наших объектов."], []
        for i, src in enumerate(page, offset+1):
            lines.append(f"{i}. <a href=\"{e(src['url'])}\">{e(clip(src['title'], 90))}</a> · "
                         f"{candidates.mentions(src['n'])} · {candidates.members_h(src['members'])} подписчиков")
            if src["ev"]:
                evidence = src["ev"][0]
                lines.append(f"«{e(clip(evidence['snip'], 140))}» <a href=\"{e(evidence['url'])}\">пост</a>")
            ref = self.ref(session, src["key"])
            if self.operator(actor) and session["private"]:
                rows.append([self.button(token, f"✅ Добавить {i}", f"approve:{ref}"),
                             self.button(token, f"🚫 Скрыть {i}", f"hide:{ref}")])
        if not page:
            lines.append("Новых кандидатов нет.")
        rows += self.page_buttons(token, "cand", offset, len(items))
        session["cand_offset"] = offset
        return self.card(token, "\n\n".join(lines), rows)

    def settings(self, token, session, actor):
        chat = self.ctx.settings.max_admin_chat_id
        recipient = next((r for r in subscriptions.rows(self.ctx) if r["id"] == "max:"+chat), None)
        enabled = set(recipient["events"] if recipient else [])
        rows = []
        if chat:
            for event, label in EVENT_LABELS.items():
                on = event in enabled
                rows.append([self.button(token, ("✅ " if on else "⬜ ") + label, f"sub:{event}:{int(not on)}")])
        rows.append([self.button(token, "📎 Файлы и доставка", "options")])
        return self.card(token, "<b>⚙️ Что присылать в группу Max</b>\nНажмите, чтобы включить или выключить. "
                         "Изменения сразу видны в веб-настройках.\n"
                         "«Дневной и вечерний» включает все отчёты после первого слота дня.\n"
                         + ("Группа: <code>"+e(chat)+"</code>" if chat else "MAX_ADMIN_CHAT_ID не задан."), rows)

    async def execute(self, token, session, actor, action, event_key, timestamp):
        parts = action.split(":")
        kind = parts[0]
        db = self.ctx.db
        mutations = {"sub", "option", "source_set", "approve", "hide", "unwatch", "report", "scan"}
        mutating = kind in mutations and len(parts) > 1
        if mutating and (not isinstance(timestamp, (int, float)) or not time.time()*1000-600000 <= timestamp <= time.time()*1000+60000):
            return ScreenResult(notification="Нажатие устарело. Нажмите кнопку ещё раз.")
        if kind in {"settings", "options", "sub", "option", "source_set", "source_add", "watch", "watch_add", "unwatch", "users"} \
                and not self.admin(actor, session):
            return ScreenResult(notification="Эта настройка доступна администратору в личке.")
        if kind in {"scan", "report", "approve", "hide"} and not self.operator(actor):
            return ScreenResult(notification="Действие доступно менеджеру или администратору.")
        if kind in {"approve", "hide"} and not session["private"]:
            return ScreenResult(notification="Решения по источникам принимайте в личке бота.")
        previous = db.one("SELECT result_json FROM action_receipts WHERE channel='MaxMenu' AND event_key=?", (event_key,)) if event_key else None
        if previous:
            saved = jload(previous["result_json"], {})
            return ScreenResult(self.home(token, actor, session), saved.get("notification", "Уже выполнено"))
        self.clear_input(actor, session["destination"])
        if kind == "skipped":
            cards = await commands.command(self.ctx, "skipped", "24", actor, self.bot.service, session["private"])
            cards[0].rows.append(self.navigation(token))
            return ScreenResult(cards[0])
        notice = ""
        with db.tx():
            if kind == "home":
                card = self.home(token, actor, session)
            elif kind in {"status", "brain", "help"}:
                text = {"status": commands.status_text, "brain": commands.brain_text}.get(kind)
                card = self.card(token, text(self.ctx) if text else commands.HELP + "\n/menu — кнопочное меню")
            elif kind in {"hot", "feed", "search"}:
                card = self.items(token, session, kind, self.number(parts, default=0))
            elif kind in {"item", "find"}:
                if kind == "item" and len(parts) == 2 and parts[1].isdigit():
                    iid = int(parts[1])
                    card = self.card(token, f"Карточка #{iid} открыта отдельным сообщением.", back=session.get("return", "home"))
                    if not db.item(iid):
                        return ScreenResult(notification="Публикация не найдена.")
                    return ScreenResult(card, "Открываю карточку", iid)
                card = self.prompt(token, session, actor, kind, "Введите номер карточки." if kind == "item" else "Введите слова для поиска в архиве.")
            elif kind in {"sources", "bad"}:
                card = self.sources(token, session, actor, self.number(parts, default=0), kind == "bad")
            elif kind == "source":
                src = db.one("SELECT * FROM sources WHERE key=? AND candidate=0", (session["refs"].get(parts[-1], ""),))
                if not src:
                    return ScreenResult(notification="Источник не найден. Откройте список заново.")
                text = f"<b>📡 {e(src['title'] or src['key'])}</b>\n<code>{e(src['key'])}</code>\n" \
                       f"Опрос: {e(src['tier'])} · {e(src['where_'])}\nСостояние: {'включён' if src['enabled'] else 'выключен'}"
                buttons = []
                if self.admin(actor, session):
                    buttons.append([self.button(token, "⏸ Выключить" if src["enabled"] else "▶️ Включить",
                                                f"source_set:{parts[-1]}:{int(not src['enabled'])}")])
                card = self.card(token, text, buttons, back="sources:0")
            elif kind == "source_set" and len(parts) == 3 and parts[2] in ("0", "1"):
                key = session["refs"].get(parts[1], "")
                db.x("UPDATE sources SET enabled=? WHERE key=? AND candidate=0", (int(parts[2]), key))
                card = self.sources(token, session, actor, 0)
                notice = "Состояние источника сохранено"
            elif kind == "source_add":
                card = self.prompt(token, session, actor, "source_add", "Введите тип и адрес источника:\n"
                                   "<code>vk https://vk.com/паблик</code>\n<code>tg https://t.me/канал</code>\n"
                                   "<code>rss https://сайт/feed</code>\nВ конце можно добавить hot, warm или cold.", "sources:0")
            elif kind == "cand":
                card = self.candidates(token, session, actor, self.number(parts, default=0))
            elif kind in {"approve", "hide"} and len(parts) == 2:
                from .. import candidates
                key = session["refs"].get(parts[1], "")
                n = candidates.decide(self.ctx, [key], kind == "approve", actor.name + " (Max)")
                notice = ("Источник добавлен" if kind == "approve" else "Кандидат скрыт") if n else "Решение уже принято"
                card = self.candidates(token, session, actor, session.get("cand_offset", 0))
            elif kind == "settings":
                card = self.settings(token, session, actor)
            elif kind == "sub" and len(parts) == 3 and parts[1] in subscriptions.ALL and parts[2] in ("0", "1"):
                chat = self.ctx.settings.max_admin_chat_id
                recipient = next((r for r in subscriptions.rows(self.ctx) if r["id"] == "max:"+chat), None)
                if not recipient:
                    return ScreenResult(notification="Админ-группа Max не настроена.")
                events = [x for x in recipient["events"] if x != parts[1]]
                subscriptions.save(self.ctx, recipient["id"], events + ([parts[1]] if parts[2] == "1" else []))
                notice = "Отправки в группу сохранены"
                card = self.settings(token, session, actor)
            elif kind == "options":
                rows = [[self.button(token, ("✅ " if getattr(self.ctx.settings.max, name) else "⬜ ")+label,
                                     f"option:{name}:{int(not getattr(self.ctx.settings.max, name))}")] for name, label in OPTIONS.items()]
                card = self.card(token, "<b>📎 Файлы и доставка Max</b>\n«Только при сбое Telegram» относится к автоматическим карточкам.", rows, "settings")
            elif kind == "option" and len(parts) == 3 and parts[1] in OPTIONS and parts[2] in ("0", "1"):
                self.ctx.set_override("max."+parts[1], parts[2] == "1")
                notice = "Настройка сохранена"
                rows = [[self.button(token, ("✅ " if getattr(self.ctx.settings.max, name) else "⬜ ")+label,
                                     f"option:{name}:{int(not getattr(self.ctx.settings.max, name))}")] for name, label in OPTIONS.items()]
                card = self.card(token, "<b>📎 Файлы и доставка Max</b>", rows, "settings")
            elif kind in {"watch", "unwatch"}:
                if kind == "unwatch":
                    name = session["refs"].get(parts[-1], "")
                    from ..settings import load_yaml
                    data = load_yaml("watchlist.yaml", {}) or {}
                    data["people"] = [x for x in data.get("people", []) if x["name"] != name]
                    self.ctx.save_watchlist(data)
                    notice = "Наблюдение снято"
                rows, lines = [], ["<b>👤 Наблюдение</b>"]
                for person in self.ctx.watch.people[:15]:
                    lines.append(e(person["name"]))
                    rows.append([self.button(token, "Снять: "+clip(person["name"], 45), "unwatch:"+self.ref(session, person["name"]))])
                if not self.ctx.watch.people:
                    lines.append("Список пуст.")
                rows.append([self.button(token, "➕ Добавить имя", "watch_add")])
                card = self.card(token, "\n".join(lines), rows)
            elif kind == "watch_add":
                card = self.prompt(token, session, actor, "watch_add", "Введите имя и фамилию человека для наблюдения по упоминаниям имени.", "watch")
            elif kind == "users":
                lines = ["<b>👥 Пользователи</b>"] + [f"{e(u['name'] or u['tg_id'])} · {e(u['role'])} · "
                           f"{'активен' if u['active'] else 'отключён'}\nTG: <code>{e(u['tg_id'])}</code> · Max: <code>{e(u['max_user_id'] or '—')}</code>"
                           for u in db.q("SELECT * FROM users ORDER BY active DESC, name LIMIT 20")]
                lines.append("Добавление и роли: Радар → Настройки → Пользователи.")
                card = self.card(token, "\n\n".join(lines))
            elif kind == "scan":
                if len(parts) == 1:
                    card = self.card(token, "<b>📡 Сбор сейчас</b>\nВыберите цикл.",
                                     [[self.button(token, "🔥 Горячие источники", "scan:hot")],
                                      [self.button(token, "📰 Обычные источники", "scan:warm")]])
                elif parts[1] in ("hot", "warm"):
                    launched = self.bot.service and self.bot.service.run_now(parts[1])
                    notice = "Сбор запущен" if launched else "Сбор уже идёт или сервис недоступен"
                    card = self.card(token, notice + ". Новые срочные публикации придут алертами.")
                else:
                    return ScreenResult(notification="Неизвестный цикл сбора.")
            elif kind == "report":
                if len(parts) == 1:
                    card = self.card(token, "<b>📊 Сформировать отчёт</b>\nВыберите период. Результат придёт в этот чат.",
                                     [[self.button(token, "6 часов", "report:6"), self.button(token, "12 часов", "report:12")],
                                      [self.button(token, "Сутки", "report:24"), self.button(token, "Неделя", "report:168")]])
                else:
                    try:
                        hours = commands.report_hours(parts[1])
                    except ValueError:
                        return ScreenResult(notification="Период должен быть от 0 до 168 часов.")
                    existing = self.active_job(actor.user_id, session["destination"])
                    if not existing:
                        job_id = hashlib.sha256(event_key.encode()).hexdigest()[:24] if event_key else secrets.token_hex(12)
                        db.kv_set("max_report_job:"+job_id, {"owner": actor.user_id, "destination": session["destination"],
                                     "private": session["private"], "hours": hours, "token": token,
                                     "status": "queued", "created": iso()})
                    notice = "Отчёт уже готовится" if existing else "Готовлю отчёт"
                    card = self.card(token, notice + ". Он придёт отдельным сообщением; меню можно продолжать использовать.")
            else:
                return ScreenResult(notification="Кнопка устарела. Откройте /menu заново.")
            self.save(token, session)
            if mutating and event_key:
                db.x("INSERT INTO action_receipts(channel,event_key,result_json,created_at) VALUES('MaxMenu',?,?,?)",
                     (event_key, jdump({"notification": notice or "Уже выполнено"}), iso()))
        return ScreenResult(card, notice)

    @staticmethod
    def number(parts, default=0):
        return min(500, max(0, int(parts[1]))) if len(parts) > 1 and parts[1].isdigit() else default

    async def callback(self, payload, actor, destination, private, key, timestamp):
        parts = payload.split(":", 2)
        if len(parts) != 3:
            return ScreenResult(notification="Откройте /menu заново.")
        token, action = parts[1:]
        session = self.session(token)
        if not session or session.get("expires", 0) < time.time():
            return ScreenResult(notification="Меню устарело. Отправьте /menu.")
        if session["owner"] != actor.user_id or session["destination"] != destination or session["private"] != private:
            return ScreenResult(notification="Откройте своё меню командой /menu.")
        if actor.role not in ("viewer", "manager", "admin"):
            return ScreenResult(notification="Нет доступа.")
        return await self.execute(token, session, actor, action, key, timestamp)

    async def consume_text(self, actor, destination, private, text, key, timestamp):
        pending = self.ctx.db.kv_get("max_input:"+self.scope(actor, destination), {})
        if not pending or pending.get("expires", 0) < time.time():
            self.clear_input(actor, destination)
            return None
        session = self.session(pending["token"])
        if not session or session["destination"] != destination or session["private"] != private or session["owner"] != actor.user_id:
            self.clear_input(actor, destination)
            return None
        if not isinstance(timestamp, (int, float)) or not time.time()*1000-600000 <= timestamp <= time.time()*1000+60000:
            return ScreenResult(notification="Ответ устарел. Откройте нужный пункт меню заново.")
        token, kind = pending["token"], pending["kind"]
        if kind == "item" and not text.isdigit():
            return ScreenResult(self.card(token, "Введите номер карточки цифрами, например 123."))
        if not text or len(text) > 500:
            return ScreenResult(self.card(token, "Введите от 1 до 500 символов."))
        self.clear_input(actor, destination)
        if kind == "find":
            session["query"] = text
            return await self.execute(token, session, actor, "search:0", key, timestamp)
        if kind == "item":
            return await self.execute(token, session, actor, "item:"+text, key, timestamp)
        if kind in ("watch_add", "source_add") and self.admin(actor, session):
            name = "watch" if kind == "watch_add" else "src_add"
            cards = await commands.command(self.ctx, name, text, actor, self.bot.service, True)
            cards[0].rows.append(self.navigation(token, "watch" if kind == "watch_add" else "sources:0"))
            return ScreenResult(cards[0])
        return ScreenResult(notification="Доступ изменился. Откройте /menu заново.")

    def active_job(self, owner, destination):
        return next((jload(r["value"], {}) for r in self.ctx.db.q("SELECT value FROM kv WHERE key LIKE 'max_report_job:%'")
                     if (j := jload(r["value"], {})).get("owner") == owner and j.get("destination") == destination
                     and j.get("status") in ("queued", "running")), None)

    def kick_reports(self):
        if self._worker is None or self._worker.done():
            if self.ctx.db.one("SELECT 1 FROM kv WHERE key LIKE 'max_report_job:%' AND "
                               "json_extract(value,'$.status') IN ('queued','running') LIMIT 1"):
                self._worker = asyncio.create_task(self.reports())

    async def reports(self):
        db = self.ctx.db
        for row in db.q("SELECT key,value FROM kv WHERE key LIKE 'max_report_job:%' ORDER BY key"):
            job = jload(row["value"], {})
            if job.get("status") not in ("queued", "running"):
                continue
            actor = self.bot.actor({"user_id": job["owner"]})
            allowed = self.operator(actor) and self.ctx.alerts.max.enabled and (
                self.ctx.settings.max.direct_messages if job["private"] else job["destination"] == self.ctx.settings.max_admin_chat_id)
            if not allowed:
                job["status"] = "cancelled"; db.kv_set(row["key"], job); continue
            job["status"] = "running"; db.kv_set(row["key"], job)
            try:
                result = await commands.build_report(self.ctx, job["hours"], self.bot.service)
                card = self.card(job["token"], result["short"])
                files = [result["xlsx"], result["docx"]] if self.ctx.settings.max.report_files else []
                if not files:
                    card.text_html += "\nExcel и Word: Радар → Отчёты."
                with db.tx():
                    self.ctx.alerts.max.enqueue_text(job["destination"], card, row["key"]+":result", files=files)
                    job["status"] = "done"; db.kv_set(row["key"], job)
            except asyncio.CancelledError:
                job["status"] = "queued"; db.kv_set(row["key"], job)
                raise
            except Exception as err:
                log.error("Max: отчёт из меню не сформирован (%s)", type(err).__name__)
                with db.tx():
                    self.ctx.alerts.max.enqueue_text(job["destination"], self.card(job["token"], "Не удалось сформировать отчёт. Повторите запрос позже."),
                                                     row["key"]+":error")
                    job["status"] = "failed"; db.kv_set(row["key"], job)

    async def close(self):
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    def cleanup(self):
        self.ctx.db.x("DELETE FROM kv WHERE key LIKE 'max_menu:%' AND json_extract(value,'$.expires')<?", (time.time(),))
        self.ctx.db.x("DELETE FROM kv WHERE key LIKE 'max_input:%' AND json_extract(value,'$.expires')<?", (time.time(),))
        self.ctx.db.x("DELETE FROM kv WHERE key LIKE 'max_report_job:%' AND json_extract(value,'$.status') NOT IN ('queued','running') "
                      "AND json_extract(value,'$.created')<?", (ago(hours=24),))
