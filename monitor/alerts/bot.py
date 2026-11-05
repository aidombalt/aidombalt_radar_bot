"""Telegram-бот: полноценное управление Радаром (дублирует GUI) + кнопки на алертах.

Доступ: только пользователи из таблицы users (или TG_ADMINS в .env). /start показывает ваш id.
Роли: viewer — читает; manager — принимает решения и запускает сбор/отчёты; admin — настройки и пользователи.
"""
from __future__ import annotations

import asyncio
import secrets
import logging
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import yaml
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, TelegramObject)

from .. import candidates as cand
from .. import linkadd as la
from ..brain.router import local_state
from ..collectors.web import gnews_pause
from ..settings import CONFIG_DIR
from ..util import ago, clip, html_escape as e, iso, jload, msk, relay_url
from . import commands as shared
from .render import Card, Btn
from .telegram_menu import TelegramMenu
from .formatting import PRIO_ICON, objects_line, tg_alert, tg_card, tg_keyboard

if TYPE_CHECKING:
    from ..context import Ctx
    from ..service import Service

log = logging.getLogger("radar.bot")

MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 Статус", callback_data="m:status"),
     InlineKeyboardButton(text="🔥 Горячее 24ч", callback_data="m:hot"),
     InlineKeyboardButton(text="📋 Лента", callback_data="m:feed")],
    [InlineKeyboardButton(text="🔎 Скан сейчас", callback_data="m:scan"),
     InlineKeyboardButton(text="🧾 Отчёт сейчас", callback_data="m:report"),
     InlineKeyboardButton(text="🧠 Мозг", callback_data="m:brain")],
    [InlineKeyboardButton(text="📡 Источники", callback_data="m:sources"),
     InlineKeyboardButton(text="🆕 Кандидаты", callback_data="m:cand"),
     InlineKeyboardButton(text="👤 Наблюдение", callback_data="m:watch")],
    [InlineKeyboardButton(text="❓ Команды", callback_data="m:help")],
    [InlineKeyboardButton(text="🧩 Повторы и отсев", callback_data="m:skipped")],
])

HELP = """<b>Команды Радара</b>
/link — привязать аккаунт Max (в личке)\n/menu — меню · /status — состояние · /hot — P1/P2 за сутки · /feed 10 — лента
/scan hot|warm|cold — внеочередной сбор · /report 24 — отчёт за N часов (Excel+Word)
/item 123 — карточка · /find текст — поиск по архиву · /replay текст — «а что бы сказал Радар?»
/sources [bad] — источники · /cand — кандидаты (одобрить/отклонить)
/src_add vk|tg|rss адрес [hot|warm|cold] — добавить источник · /src_off ключ · /src_on ключ
/watch Имя Фамилия [vk_id] — на контроль · /unwatch Имя · /kw_add фраза · /kw_del фраза
/brain — статус мозга (Colab/локальный) · /skipped 24 — не присланные: чужой дом в нашем ЖК, копии отмеченных историй
📥 <b>Перешлите боту любой пост или сообщение из домового чата</b> — он уйдёт в анализ.
🔗 <b>Пришлите ссылку</b> на Telegram-канал, паблик или страницу ВК — бот покажет, что это, и предложит добавить
источник или поставить человека на контроль.
<b>Админ:</b> /set путь значение (напр. <code>/set schedule.hot_minutes 5</code>) · /users · /adduser id роль [email] · /deluser id · /test"""


class Access(BaseMiddleware):
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject,
                       data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        uid = str(user.id) if user else ""
        admins = {x.strip() for x in os.environ.get("TG_ADMINS", "").split(",") if x.strip()}
        row = self.ctx.db.one("SELECT * FROM users WHERE tg_id=? AND active=1", (uid,))
        role = "admin" if uid in admins else (row["role"] if row else None)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            data["role"] = role
            return await handler(event, data)
        if not role:
            if isinstance(event, Message):
                await event.answer(f"⛔️ Нет доступа. Ваш Telegram id: <code>{uid}</code> — передайте администратору Радара.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None
        data["role"] = role
        data["uname"] = (user.full_name if user else uid) or uid
        return await handler(event, data)


async def answer(c: CallbackQuery, text: str | None = None, **kw) -> None:
    """Всплывашка на кнопке. Если нажатие дошло с опозданием («query is too old»), действие всё равно выполнено —
    просто молча пропускаем подсказку, а не роняем обработчик."""
    try:
        await c.answer(text, **kw)
    except TelegramBadRequest as ex:
        log.info("кнопка: подсказка не показана (%s)", ex.message)


def build_bot(ctx: "Ctx", service: "Service", session=None, token: str | None = None) -> tuple[Bot, Dispatcher] | None:
    token = token or ctx.settings.tg_bot_token
    if not token:
        log.warning("TG_BOT_TOKEN не задан — бот выключен")
        return None
    proxy, relay = ctx.settings.network.telegram_proxy, relay_url()
    if session is None:
        if proxy:
            session = AiohttpSession(proxy=proxy, timeout=20)
        elif relay:   # Telegram закрыт в сети офиса — ходим через свой релей на Cloudflare
            session = AiohttpSession(api=TelegramAPIServer.from_base(f"{relay}/api.telegram.org"), timeout=20)
            session._connector_init["force_close"] = True   # каждое обращение — новое соединение: зависшее не копится
        else:
            session = AiohttpSession(timeout=20)
    bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                         link_preview_is_disabled=True))
    dp = Dispatcher()
    r = Router()
    r.message.middleware(Access(ctx))
    r.callback_query.middleware(Access(ctx))
    db = ctx.db
    panel = TelegramMenu(ctx, bot, MENU)
    reports_running: set[tuple[int, int]] = set()

    # ------------------------------------------------------------ тексты
    def dynamics_text(iid: int, fresh: bool) -> str:
        return shared.dynamics_text(ctx, iid, fresh)

    def status_text() -> str:
        return shared.status_text(ctx)

    def item_short(it) -> str:
        return shared.item_short(ctx, it)

    async def send_long(m: Message, parts: list[str], empty: str = "Пусто."):
        if not parts:
            await m.answer(empty)
            return
        buf = ""
        for p in parts:
            if len(buf) + len(p) > 3800:
                await m.answer(buf)
                buf = ""
            buf += p + "\n\n"
        if buf:
            await m.answer(buf)

    def is_admin(role: str | None) -> bool:
        return role == "admin"

    # ------------------------------------------------------------ команды
    @r.message(CommandStart())
    async def start(m: Message, role: str | None = None):
        if role and ctx.alerts.recipient_ready(m.chat.id):
            asyncio.create_task(ctx.alerts.process())
        group = f"\nid этой группы: <code>{m.chat.id}</code> — впишите его в TG_ADMINS, чтобы алерты шли сюда." \
            if m.chat.type in ("group", "supergroup") else ""
        text = f"Радар упоминаний {e(ctx.settings.company)}.\nВаш id: <code>{m.from_user.id}</code>{group}\n" \
               + ("Доступ есть, роль: <b>" + role + "</b>" if role else "Доступа пока нет — передайте id администратору.")
        if role:
            await panel.publish(m, role, [Card(text)], root=True, screen="home")
        else:
            await m.answer(text)

    @r.message(Command("link"))
    async def account_link(m: Message, role: str = ""):
        if m.chat.type != "private":
            return await m.answer("Получите код в личке Telegram-бота")
        code = shared.link_code(ctx, shared.Actor(str(m.from_user.id), m.from_user.full_name, role, "Telegram"))
        await m.answer("В течение 10 минут отправьте в личку Max-бота:\n<code>/link " + e(code) + "</code>\nКод одноразовый.")

    @r.message(Command("menu"))
    async def menu(m: Message, role: str = "viewer"):
        panel.cleanup()
        # An explicit /menu brings a fresh panel below intervening alerts; its buttons edit in place.
        await panel.publish(m, role, [Card("<b>Меню Радара</b>\nВыберите действие.")], root=True, screen="home", force_new=True)

    @r.message(Command("help"))
    async def help_(m: Message, role: str = "viewer"):
        await panel.publish(m, role, [Card(HELP)], screen="help")

    @r.message(Command("status"))
    async def status(m: Message, role: str = "viewer"):
        await panel.publish(m, role, [Card(status_text())], screen="status")

    @r.message(Command("hot"))
    async def hot(m: Message, role: str = ""):
        await panel.publish(m, role, panel.items("hot"), screen="hot")

    @r.message(Command("feed"))
    async def feed(m: Message, command: CommandObject, role: str = ""):
        await panel.publish(m, role, panel.items("feed", command.args or ""), screen="feed")

    @r.message(Command("item"))
    async def item(m: Message, command: CommandObject, role: str = ""):
        actor = shared.Actor(str(m.from_user.id), m.from_user.full_name, role or "viewer", "Telegram")
        cards = await shared.command(ctx, "item", (command.args or ""), actor, service, m.chat.type == "private")
        await panel.publish(m, role, cards, screen="item:"+(command.args or ""))

    @r.message(Command("find"))
    async def find(m: Message, command: CommandObject, role: str = ""):
        cards = panel.items("find", command.args) if command.args else [Card("Поиск: /find текст")]
        await panel.publish(m, role, cards, screen="find")

    @r.message(Command("replay"))
    async def replay(m: Message, command: CommandObject):
        if not command.args:
            return await m.answer("Вставьте текст публикации после /replay — Радар покажет, как бы он её оценил.")
        from ..tools_replay import replay_text

        await m.answer("⏳ Разбираю…")
        res = await replay_text(ctx, service, command.args)
        await m.answer(res)

    @r.message(Command("scan"))
    async def scan(m: Message, command: CommandObject, role: str = ""):
        actor = shared.Actor(str(m.from_user.id), m.from_user.full_name, role or "viewer", "Telegram")
        cards = await shared.command(ctx, "scan", (command.args or ""), actor, service, m.chat.type == "private")
        await panel.publish(m, role, cards, screen="scan")

    @r.message(Command("report"))
    async def report(m: Message, command: CommandObject, role: str = "", callback: CallbackQuery | None = None):
        if role not in ("admin", "manager"):
            return await panel.publish(m, role, [Card("Только менеджер или администратор")])
        try:
            hours = shared.report_hours(command.args or "")
        except ValueError:
            cards = [Card("Укажите период от 0 до 168 часов: /report 24")]
            return await panel.callback(callback, role, cards) if callback else await panel.publish(m, role, cards)
        uid = callback.from_user.id if callback else m.from_user.id
        task_key = (m.chat.id, uid)
        if task_key in reports_running:
            cards = [Card("Отчёт уже готовится. Результат придёт после завершения.")]
            return await panel.callback(callback, role, cards) if callback else await panel.publish(m, role, cards)
        reports_running.add(task_key)
        try:
            cards = [Card(f"🧾 Готовлю отчёт за {hours:g} ч…\nМожно продолжать пользоваться меню.")]
            target = await panel.callback(callback, role, cards, screen="report") if callback else await panel.publish(m, role, cards, screen="report")
            mid, revision = target
            res = await shared.build_report(ctx, hours, service)
            user = db.one("SELECT role FROM users WHERE tg_id=? AND active=1", (str(uid),))
            admins = {x.strip() for x in os.environ.get("TG_ADMINS", "").split(",") if x.strip()}
            current_role = "admin" if str(uid) in admins else (user["role"] if user else "")
            if current_role not in ("manager", "admin"):
                await panel.show(m.chat.id, uid, [Card("Отправка отчёта отменена: доступ пользователя изменился.")],
                                 mid=mid, role=current_role or "viewer")
                return
            note = ""
            files = [(res["xlsx"], f"Excel · значимых: {res['items']}"), (res["docx"], f"Word · текст: {res['narrative_by']}")]
            delivered = False
            if not (relay_url() and not ctx.settings.network.telegram_proxy):
                try:
                    for path, cap in files:
                        await bot.send_document(m.chat.id, FSInputFile(path), caption=cap, request_timeout=90)
                    delivered = True
                except TelegramNetworkError:
                    log.warning("отчёт в Telegram не загрузился — отправляю файлы почтой")
            if not delivered:
                u = db.one("SELECT email FROM users WHERE tg_id=? AND email<>''", (str(uid),))
                mails = [u["email"]] if u and u["email"] else ctx.alerts.email_recipients()
                for mail in mails:
                    ctx.alerts.enqueue("email", mail, {"to": [mail], "subject": res["subject"] + " (по запросу из бота)",
                                                       "html": res["html"], "attachments": [res["xlsx"], res["docx"]]},
                                       f"report:{res['id']}:{mail}")
                note = ("\n\n📎 Excel и Word отправил на почту: " + e(", ".join(mails)) + ". Файлы также лежат в Радаре → «Отчёты».") \
                       if mails else "\n\n📎 Файлы — в Радаре → «Отчёты». Почта для отчётов не задана (Настройки)."
            current = panel.state(m.chat.id, mid)
            still_report = current.get("revision") == revision
            await panel.show(m.chat.id, uid, [Card(res["short"]+note)], mid=mid if still_report else None,
                             force_new=not still_report, bind_active=still_report, role=role, screen="report_done")
        finally:
            reports_running.discard(task_key)

    @r.message(Command("brain"))
    async def brain(m: Message, role: str = "viewer"):
        await panel.publish(m, role, [Card(shared.brain_text(ctx))], screen="brain")

    @r.message(Command("sources"))
    async def sources(m: Message, command: CommandObject, role: str = "viewer"):
        bad = (command.args or "").strip() == "bad"
        rows = db.q("SELECT * FROM sources WHERE candidate=0 " + ("AND enabled=1 AND consecutive_failures>=3 " if bad else "")
                    + "ORDER BY enabled DESC, tier, kind, key LIMIT 80")
        parts = [f"{'🟢' if s['enabled'] and (s['consecutive_failures'] or 0) < 3 else '🔴' if s['enabled'] else '⏸'} "
                 f"<code>{e(s['key'][:60])}</code> {e(clip(s['title'] or '', 40))} · {s['tier']}/{s['where_']}"
                 + (f"\n   ⚠️ {e(clip(s['last_error'], 120))}" if s["last_error"] and (s["consecutive_failures"] or 0) >= 3 else "")
                 for s in rows]
        await panel.publish(m, role, shared.chunks(parts, "Источников нет"), screen="sources")

    # ------------------------------------------------------------ кандидаты: одно сообщение, отметки номерами
    cand_views: dict[str, dict] = {}
    CAND_PAGE = 6

    def cand_render(tok: str) -> tuple[str, InlineKeyboardMarkup | None]:
        v = cand_views[tok]
        page = v["items"][v["off"]: v["off"] + CAND_PAGE]
        total = len(v["items"])
        lines = [f"🆕 <b>Кандидаты в источники</b> · {v['off'] + 1}–{v['off'] + len(page)} из {total}",
                 "<i>Паблики ВК, где поиск нашёл посты про наши дома. Отметьте номера и нажмите «Добавить» или «Скрыть».</i>"]
        for i, c in enumerate(page, 1):
            done = v["done"].get(c["key"])
            mark = " — ✅ добавлен" if done == "ok" else " — 🚫 скрыт" if done == "no" else ""
            lines.append(f"\n<b>{i}.</b> <a href=\"{e(c['url'])}\">{e(clip(c['title'], 60))}</a> · "
                         f"{cand.members_h(c['members'])}{mark}")
            if c["ev"]:
                ev = c["ev"][0]
                lines.append(f"    {cand.mentions(c['n'])} · последнее {cand.when(c['last'])} · {e(', '.join(c['objs']))}")
                lines.append(f"    «{e(clip(ev['snip'], 110))}» <a href=\"{e(ev['url'])}\">пост</a>")
        rows, nums = [], []
        for i, c in enumerate(page, 1):
            done = v["done"].get(c["key"])
            label = f"✅ {i}" if done == "ok" else f"🚫 {i}" if done == "no" else (
                f"☑️ {i}" if c["key"] in v["sel"] else f"▫️ {i}")
            nums.append(InlineKeyboardButton(text=label, callback_data=f"k:{tok}:{'x' if done else 't'}{i}"))
        rows += [nums[:3], nums[3:]] if len(nums) > 3 else [nums]
        n = sum(1 for c in page if c["key"] in v["sel"])
        if any(c["key"] not in v["done"] for c in page):
            rows.append([InlineKeyboardButton(text=f"✅ Добавить ({n})", callback_data=f"k:{tok}:add"),
                         InlineKeyboardButton(text=f"🚫 Скрыть ({n})", callback_data=f"k:{tok}:hide")])
        nav = []
        if v["off"] > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"k:{tok}:pv"))
        if v["off"] + CAND_PAGE < total:
            nav.append(InlineKeyboardButton(text=f"Ещё {min(CAND_PAGE, total - v['off'] - CAND_PAGE)} ➡️",
                                            callback_data=f"k:{tok}:nx"))
        if nav:
            rows.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[r_ for r_ in rows if r_])

    @r.message(Command("cand"))
    async def cand_cmd(m: Message, role: str = "viewer", callback: CallbackQuery | None = None):
        items = cand.listing(ctx)
        if not items:
            cards = [Card("Новых кандидатов нет. Автопоиск предлагает паблики ВК, только если в них нашёлся "
                          "пост про наш адрес, УК или ЖК в нашем районе.")]
            return await panel.callback(callback, role, cards, screen="cand") if callback else await panel.publish(m, role, cards, screen="cand")
        tok = secrets.token_hex(3)
        cand_views[tok] = {"items": items, "off": 0, "sel": set(), "done": {}}
        cand_views[tok]["owner"] = callback.from_user.id if callback else m.from_user.id
        while len(cand_views) > 20:
            cand_views.pop(next(iter(cand_views)))
        text, kb = cand_render(tok)
        cards = [Card.from_tg(text, kb.model_dump(exclude_none=True))]
        if callback:
            await panel.callback(callback, role, cards, screen="cand")
        else:
            await panel.publish(m, role, cards, screen="cand")

    @r.callback_query(F.data.startswith("k:"))
    async def on_cand_view(c: CallbackQuery, uname: str = "", role: str | None = None):
        _, tok, act = c.data.split(":", 2)
        v = cand_views.get(tok)
        if not v:
            await answer(c, "Список устарел — откройте «Кандидаты» заново")
            return await panel.callback(c, role, [Card("Список устарел. Откройте «Кандидаты» заново.", [[Btn("🆕 Кандидаты", "m:cand")]])])
        if v.get("owner") != c.from_user.id:
            return await answer(c, "Откройте своё меню командой /menu", show_alert=True)
        page = v["items"][v["off"]: v["off"] + CAND_PAGE]
        if act.startswith("x"):
            return await answer(c, "Уже решено")
        if act.startswith("t"):
            key = page[int(act[1:]) - 1]["key"]
            v["sel"].symmetric_difference_update({key})
            await answer(c)
        elif act in ("add", "hide"):
            if role not in ("admin", "manager"):
                return await answer(c, "Решать могут менеджеры и администраторы")
            keys = [x["key"] for x in page if x["key"] in v["sel"]]
            if not keys:
                return await answer(c, "Сначала отметьте номера")
            ok = act == "add"
            n = cand.decide(ctx, keys, ok, by=uname)
            v["done"].update({k: "ok" if ok else "no" for k in keys})
            v["sel"] -= set(keys)
            await answer(c, f"{'Добавлено' if ok else 'Скрыто'}: {n}" + (". Первый опрос — в ближайшем цикле" if ok else ""))
        elif act in ("nx", "pv"):
            v["off"] = max(0, v["off"] + (CAND_PAGE if act == "nx" else -CAND_PAGE))
            await answer(c)
        text, kb = cand_render(tok)
        await panel.callback(c, role, [Card.from_tg(text, kb.model_dump(exclude_none=True))], screen="cand")

    @r.message(Command("src_add"))
    async def src_add(m: Message, command: CommandObject, role: str):
        parts = (command.args or "").split()
        if len(parts) < 2:
            return await m.answer("Формат: /src_add vk|tg|rss адрес [hot|warm|cold]\nнапр. /src_add vk murino_live warm")
        kind = {"vk": "vk_wall", "tg": "tg_channel", "rss": "rss"}.get(parts[0])
        if not kind:
            return await m.answer("Тип: vk, tg или rss")
        handle = parts[1].replace("https://vk.com/", "").replace("https://t.me/", "").lstrip("@")
        tier = parts[2] if len(parts) > 2 and parts[2] in ("hot", "warm", "cold") else "warm"
        key = f"{kind}:{handle}"
        db.upsert_source({"key": key, "kind": kind, "handle": handle, "title": handle, "tier": tier,
                          "category": "public", "comments": kind == "vk_wall",
                          "where": "colab" if kind == "tg_channel" else "server"})
        await m.answer(f"✅ Добавлен <code>{e(key)}</code> ({tier}). Проверю при следующем цикле.")

    @r.message(Command("src_off", "src_on"))
    async def src_toggle(m: Message, command: CommandObject):
        key = (command.args or "").strip()
        n = db.x("UPDATE sources SET enabled=? WHERE key=?", (int(command.command == "src_on"), key))
        await m.answer("Готово" if n else "Нет такого ключа (см. /sources)")

    @r.message(Command("watch"))
    async def watch(m: Message, command: CommandObject, role: str | None = None):
        if command.args and not isinstance(la.parse(command.args.strip()), str):
            return await link_flow(m, command.args.strip(), role)      # /watch vk.com/… — по ссылке
        if not command.args:
            names = ", ".join(p["name"] for p in ctx.watch.people) or "никого"
            return await m.answer(f"На контроле: {e(names)}\nДобавить: пришлите ссылку на страницу ВК или @ник в Telegram "
                                  "(или /watch Имя Фамилия — следить только по имени)")
        parts = command.args.split()
        vk = parts[-1] if len(parts) > 2 and (parts[-1].isdigit() or parts[-1].startswith("id")) else ""
        name = " ".join(parts[:-1] if vk else parts)
        data = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
        data.setdefault("people", []).append({"name": name, "aliases": [], "vk_id": vk, "tg": "", "note": f"добавил {m.from_user.full_name}"})
        ctx.save_watchlist(data)
        ctx.sync_sources_from_yaml()
        await m.answer(f"👤 {e(name)} на контроле" + (f", стена ВК {e(vk)} опрашивается в «горячем» цикле" if vk else ""))

    @r.message(Command("unwatch"))
    async def unwatch(m: Message, command: CommandObject):
        data = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
        before = len(data.get("people", []))
        data["people"] = [p for p in data.get("people", []) if p["name"].lower() != (command.args or "").lower()]
        ctx.save_watchlist(data)
        await m.answer("Снято с контроля" if len(data["people"]) < before else "Не найдено")

    @r.message(Command("kw_add", "kw_del"))
    async def kw(m: Message, command: CommandObject):
        data = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
        kws = data.setdefault("keywords", []) or []
        phrase = (command.args or "").strip()
        if not phrase:
            return await m.answer("Ключевые фразы: " + (e(", ".join(kws)) or "нет"))
        if command.command == "kw_add" and phrase not in kws:
            kws.append(phrase)
        elif command.command == "kw_del" and phrase in kws:
            kws.remove(phrase)
        data["keywords"] = kws
        ctx.save_watchlist(data)
        service.register_virtual_sources()
        await m.answer("Готово. Фраза учитывается матчером и поиском ВК/Google News.")

    @r.message(Command("set"))
    async def set_(m: Message, command: CommandObject, role: str):
        if not is_admin(role):
            return await m.answer("Только для администратора")
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            return await m.answer("Формат: /set schedule.hot_minutes 5")
        val: Any = parts[1]
        try:
            val = yaml.safe_load(parts[1])
        except Exception:
            pass
        try:
            ctx.set_override(parts[0], val)
        except Exception as ex:
            return await m.answer(f"Ошибка: {e(str(ex))}")
        service._build_jobs()
        await m.answer(f"✅ {e(parts[0])} = {e(str(val))}")

    @r.message(Command("users"))
    async def users(m: Message, role: str):
        if not is_admin(role):
            return await m.answer("Только для администратора")
        rows = db.q("SELECT * FROM users ORDER BY role, name")
        await m.answer("\n".join(f"{'🟢' if u['active'] else '⚪️'} <code>{u['tg_id']}</code> {e(u['name'] or '')} · {u['role']} "
                                 f"· ур.{u['level']} {e(u['email'] or '')}" for u in rows) or "Пусто")

    @r.message(Command("adduser"))
    async def adduser(m: Message, command: CommandObject, role: str):
        if not is_admin(role):
            return await m.answer("Только для администратора")
        p = (command.args or "").split()
        if len(p) < 2 or p[1] not in ("viewer", "manager", "admin"):
            return await m.answer("Формат: /adduser 123456789 viewer|manager|admin [email]")
        db.x("INSERT INTO users(tg_id, name, role, email, level, active, added_at) VALUES(?,?,?,?,?,1,?) "
             "ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role, email=excluded.email, level=excluded.level, active=1",
             (p[0], p[0], p[1], p[2] if len(p) > 2 else "", 2 if p[1] in ("manager", "admin") else 1, iso()))
        await m.answer("✅ Пользователь добавлен")

    @r.message(Command("deluser"))
    async def deluser(m: Message, command: CommandObject, role: str):
        if not is_admin(role):
            return await m.answer("Только для администратора")
        db.x("UPDATE users SET active=0 WHERE tg_id=?", ((command.args or "").strip(),))
        await m.answer("Отключён")

    @r.message(Command("test"))
    async def test(m: Message):
        await m.answer("🔴 <b>Тестовый алерт</b>\nЕсли вы это видите — доставка работает.", reply_markup=MENU)

    # ------------------------------------------------------------ кнопки меню
    @r.callback_query(F.data.startswith("m:"))
    async def on_menu(c: CallbackQuery, role: str):
        if not c.message:
            return await answer(c, "Откройте /menu заново")
        saved = panel.state(c.message.chat.id, c.message.message_id)
        if saved and saved["owner"] != str(c.from_user.id):
            return await answer(c, "Откройте своё меню командой /menu", show_alert=True)
        what, _, args = c.data[2:].partition(":")
        if what in ("scan", "report") and role not in ("manager", "admin"):
            return await answer(c, "Только менеджер или администратор", show_alert=True)
        if what in ("scan", "report") and args:
            if db.one("SELECT 1 FROM action_receipts WHERE channel='TelegramMenu' AND event_key=?", (str(c.id),)):
                return await answer(c, "Уже выполнено")
            db.x("INSERT INTO action_receipts(channel,event_key,result_json,created_at) VALUES('TelegramMenu',?,'{}',?)", (str(c.id), iso()))
        await answer(c)
        m = c.message
        actor = shared.Actor(str(c.from_user.id), c.from_user.full_name, role, "Telegram")
        if what == "home":
            return await panel.callback(c, role, [Card("<b>Меню Радара</b>\nВыберите действие.")], root=True, screen="home")
        if what == "page":
            return await panel.page(c, role, int(args)) if args.isdigit() else None
        if what == "parent":
            if await panel.parent(c, role):
                return
            what = "feed"
        if what == "item":
            if args.isdigit():
                return await panel.item(c, role, int(args))
            return
        if what == "status":
            await panel.callback(c, role, [Card(status_text())], screen="status")
        elif what in ("hot", "feed"):
            await panel.callback(c, role, panel.items(what), screen=what)
        elif what == "scan":
            cards = await shared.command(ctx, "scan", args, actor, service) if args else [Card("<b>Сбор сейчас</b>\nВыберите цикл.",
                         [[Btn("🔥 Горячие", "m:scan:hot"), Btn("📰 Обычные", "m:scan:warm")], [Btn("📚 Полный", "m:scan:cold")]])]
            await panel.callback(c, role, cards, screen="scan")
        elif what == "report":
            if args:
                fake = type("C", (), {"args": args})()
                await report(m, fake, role, callback=c)
            else:
                await panel.callback(c, role, [Card("<b>Сформировать отчёт</b>\nВыберите период.",
                        [[Btn("6 часов", "m:report:6"), Btn("12 часов", "m:report:12")],
                         [Btn("Сутки", "m:report:24"), Btn("Неделя", "m:report:168")]])], screen="report")
        elif what == "brain":
            await panel.callback(c, role, [Card(shared.brain_text(ctx))], screen="brain")
        elif what == "sources":
            rows = db.q("SELECT * FROM sources WHERE candidate=0 ORDER BY enabled DESC,tier,kind,key LIMIT 200")
            parts = [f"{'🟢' if s['enabled'] and (s['consecutive_failures'] or 0)<3 else '🔴' if s['enabled'] else '⏸'} "
                     f"<code>{e(s['key'][:60])}</code> {e(clip(s['title'] or '', 40))} · {s['tier']}/{s['where_']}" for s in rows]
            await panel.callback(c, role, shared.chunks(parts, "Источников нет"), screen="sources")
        elif what == "cand":
            await cand_cmd(m, role, callback=c)
        elif what == "watch":
            names = ", ".join(p["name"] for p in ctx.watch.people) or "никого"
            await panel.callback(c, role, [Card(f"<b>Люди на контроле</b>\n{e(names)}\nДобавить: пришлите ссылку на ВК или /watch Имя Фамилия.")], screen="watch")
        elif what == "help":
            await panel.callback(c, role, [Card(HELP)], screen="help")
        elif what == "skipped":
            await panel.callback(c, role, await shared.command(ctx, "skipped", "24", actor, service), screen="skipped")
        else:
            await panel.callback(c, role, [Card("Кнопка устарела. Вернитесь в меню.")])

    # ------------------------------------------------------------ кнопки алертов
    @r.callback_query(F.data.startswith("a:"))
    async def on_alert(c: CallbackQuery, uname: str = "", role: str = ""):
        owned = panel.state(c.message.chat.id, c.message.message_id) if c.message else {}
        if owned and owned["owner"] != str(c.from_user.id):
            return await answer(c, "Откройте своё меню командой /menu", show_alert=True)
        actor = shared.Actor(str(c.from_user.id), uname, role, "Telegram")
        result = await shared.handle_action(ctx, c.data or "", actor, str(c.id), service)
        await answer(c, result.notification[:190], show_alert=not result.item_id)
        if not result.item_id or not c.message:
            return
        iid = result.item_id
        if panel.state(c.message.chat.id, c.message.message_id):
            if result.reply_html:
                await panel.callback(c, role, [Card(result.reply_html)], back=f"item:{iid}", screen=f"detail:{iid}")
            else:
                await panel.item(c, role, iid, result.state)
                await ctx.alerts.refresh_cards(iid, result.state)
            return
        if result.reply_html:
            await panel.edit_raw(c.message.chat.id, c.message.message_id,
                                 Card(result.reply_html, [[Btn("⬅️ К карточке", f"a:card:{iid}")]]))
            return
        db.x("INSERT OR IGNORE INTO alert_msgs(item_id,chat_id,message_id,sent_at) VALUES(?,?,?,?)",
             (iid, str(c.message.chat.id), c.message.message_id, iso()))
        if not ctx.settings.tg_bot_token:
            card = shared.card_for(ctx, iid, result.state)
            await c.message.edit_text(card.text_html, reply_markup=InlineKeyboardMarkup.model_validate(card.telegram()["reply_markup"]))
            await ctx.alerts.max.refresh(iid, result.state)
        else:
            await ctx.alerts.refresh_cards(iid, result.state)

    # ------------------------------------------------------------ «🏠 Наш дом» / «🚫 Не наш дом» / «✅ Наше» (дизайн §6)
    @r.callback_query(F.data.startswith("h:"))
    async def on_house(c: CallbackQuery, uname: str = "", role: str = ""):
        owned = panel.state(c.message.chat.id, c.message.message_id) if c.message else {}
        if owned and owned["owner"] != str(c.from_user.id):
            return await answer(c, "Откройте своё меню командой /menu", show_alert=True)
        result = await shared.handle_action(ctx, c.data or "", shared.Actor(str(c.from_user.id), uname, role, "Telegram"),
                                            str(c.id), service)
        await answer(c, result.notification[:190], show_alert=len(result.notification) > 60)
        if result.choices and c.message:
            kb = [[InlineKeyboardButton(text=ctx.reg.company_name(k)[:40], callback_data=f"h:our:{result.item_id}:{k}")]
                  for k in result.choices]
            card = Card.from_tg(e(result.notification), {"inline_keyboard": [[b.model_dump(exclude_none=True) for b in row] for row in kb]})
            if panel.state(c.message.chat.id, c.message.message_id):
                await panel.callback(c, role, [card], back=f"item:{result.item_id}", screen=f"detail:{result.item_id}")
            else:
                card.rows.append([Btn("⬅️ К карточке", f"a:card:{result.item_id}")])
                await panel.edit_raw(c.message.chat.id, c.message.message_id, card)
        elif result.item_id:
            if c.message and panel.state(c.message.chat.id, c.message.message_id):
                await panel.item(c, role, result.item_id)
            await ctx.alerts.refresh_cards(result.item_id)

    @r.message(Command("skipped"))
    async def skipped(m: Message, command: CommandObject, role: str = ""):
        actor = shared.Actor(str(m.from_user.id), m.from_user.full_name, role or "viewer", "Telegram")
        cards = await shared.command(ctx, "skipped", (command.args or ""), actor, service, m.chat.type == "private")
        await panel.publish(m, role, cards, screen="skipped")

    @r.callback_query(F.data.startswith("c:"))
    async def on_cand(c: CallbackQuery, role: str = ""):
        if role not in ("manager", "admin"):
            return await answer(c, "Только менеджер или администратор", show_alert=True)
        _, action, key = c.data.split(":", 2)
        if action == "ok":
            db.x("UPDATE sources SET candidate=0, enabled=1 WHERE key LIKE ?", (key + "%",))
            await answer(c, "Добавлен в мониторинг")
        else:
            db.x("UPDATE sources SET candidate=2, enabled=0 WHERE key LIKE ?", (key + "%",))
            await answer(c, "Отклонён")
        await c.message.edit_reply_markup(reply_markup=None)

    # ------------------------------------------------------------ пересланное боту → в анализ
    # ------------------------------------------------------------ ссылка → источник или человек на контроль
    pending: dict[str, la.Found] = {}

    async def link_flow(m: Message, link: str, role: str | None):
        if role not in ("admin", "manager"):
            return await m.answer("Добавлять источники и людей могут менеджеры и администраторы.")
        wait = await m.answer("🔎 Смотрю, что это…")
        f = await la.preview(ctx, service, link)
        if f.error:
            return await wait.edit_text(f"⚠️ {e(f.error)}")
        pid = secrets.token_hex(4)
        pending[pid] = f
        while len(pending) > 50:
            pending.pop(next(iter(pending)))
        lines = [f"<b>{e(f.title or f.handle)}</b>", e(f.summary())]
        if f.sample:
            lines.append(f"<i>«{e(clip(f.sample, 200))}»</i>")
        lines += [f"ℹ️ {e(n)}" for n in f.notes]
        who = la.watched_by_link(ctx, f)
        if who:
            lines.append(f"✅ Уже на контроле: {e(who)}")
            return await wait.edit_text("\n".join(lines))
        if f.person:
            kb = [[InlineKeyboardButton(text="👤 Поставить на контроль", callback_data=f"l:ppl:{pid}")],
                  [InlineKeyboardButton(text="Отмена", callback_data=f"l:no:{pid}")]]
        elif f.exists:
            lines.append("✅ Этот источник уже отслеживается.")
            return await wait.edit_text("\n".join(lines))
        else:
            lines.append(f"Определил: {e(la.CATEGORIES.get(f.category, f.category))}"
                         + (f" · {'Ленобласть' if f.region == 'LO' else 'Петербург'}" if f.region else ""))
            if la.similar_title(ctx, f.title):
                lines.append("⚠️ Источник с таким названием уже есть — возможно, это дубль.")
            kb = [[InlineKeyboardButton(text="🔥 Добавить: каждые 5 мин", callback_data=f"l:hot:{pid}")],
                  [InlineKeyboardButton(text="⏱ Добавить: каждые 15 мин", callback_data=f"l:warm:{pid}")],
                  [InlineKeyboardButton(text="Отмена", callback_data=f"l:no:{pid}")]]
        await wait.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
                             disable_web_page_preview=True)

    @r.message(Command("add"))
    async def add_cmd(m: Message, command: CommandObject, role: str | None = None):
        if not command.args:
            return await m.answer("Пришлите: /add ссылка — например /add t.me/kudrovo_life или /add vk.com/murino_live")
        await link_flow(m, command.args.strip(), role)

    @r.callback_query(F.data.startswith("l:"))
    async def on_link(c: CallbackQuery, uname: str = "", role: str | None = None):
        if role not in ("manager", "admin"):
            return await answer(c, "Только менеджер или администратор", show_alert=True)
        _, action, pid = c.data.split(":")
        f = pending.pop(pid, None)
        if action == "no" or not f:
            await answer(c, "Отменено" if action == "no" else "Устарело — пришлите ссылку ещё раз")
            return await c.message.edit_reply_markup(reply_markup=None)
        if action == "ppl":
            name = f.title or f.handle
            la.add_person(ctx, [f], name=name, aliases=la.default_aliases(name), by=uname)
            await answer(c, "Поставлено на контроль")
            text = f"👤 <b>{e(name)}</b> на контроле ({e(uname)}). Имя, другие написания и заметку можно дополнить на вкладке «Наблюдение»."
        else:
            key = la.add_source(ctx, f, tier=action, by=uname)
            await answer(c, "Добавлено")
            text = (f"✅ <b>{e(f.title)}</b> добавлен в мониторинг: {la.TIERS[action]}, "
                    f"{e(la.CATEGORIES.get(f.category, f.category))} ({e(uname)}). Первый опрос — в ближайшем цикле.")
            src = db.one("SELECT * FROM sources WHERE key=?", (key,))
            coll = service.collectors.get(src["kind"]) if src else None
            if coll:
                asyncio.create_task(coll.run([{**dict(src), "breaker_until": None}]))
        await c.message.edit_text(text, disable_web_page_preview=True)

    @r.message(F.text | F.caption)
    async def forwarded(m: Message, uname: str = "", role: str | None = None):
        text = (m.text or m.caption or "").strip()
        if text.startswith("/"):
            return await m.answer("Неизвестная команда. /help — список команд.")
        fo = getattr(m, "forward_origin", None)
        if fo is None and len(text.split()) == 1 and not isinstance(la.parse(text), str):
            return await link_flow(m, text, role)     # прислали просто ссылку — предлагаем добавить
        origin = ""
        if fo is not None:
            origin = (getattr(getattr(fo, "sender_user", None), "full_name", None)
                      or getattr(getattr(fo, "chat", None), "title", None)
                      or getattr(fo, "sender_user_name", None) or "")
        if not db.one("SELECT 1 FROM sources WHERE key='manual:forward'"):
            db.upsert_source({"key": "manual:forward", "kind": "manual", "handle": "telegram-bot", "enabled": 0,
                              "title": "Переслано сотрудниками в бот", "tier": "cold", "category": "public"})
        src = {"key": "manual:forward", "category": "public", "region": "", "title": "Переслано в бот"}
        ext = f"fwd:{m.chat.id}:{m.message_id}"
        ctx.pipeline.ingest([{"platform": "manual", "external_id": ext, "url": "", "author_id": f"fwd:{origin or uname}",
                              "author_name": origin or f"переслал(а) {uname}", "text": text, "published_at": iso()}], src)
        row = db.one("SELECT id, candidate FROM items WHERE platform='manual' AND external_id=?", (ext,))
        if not row:
            return await m.answer("Не удалось принять сообщение (пустое или в стоп-листе).")
        if not row["candidate"]:        # сотрудник прислал сам — разбираем в любом случае
            db.x("UPDATE items SET candidate=1 WHERE id=?", (row["id"],))
            db.enqueue(row["id"], 0)
        await m.answer(f"📥 Принято в анализ: #{row['id']}. Если это P1/P2 — придёт алерт. Карточка: /item {row['id']}")

    @dp.errors()
    async def on_error(ev: ErrorEvent):
        """Ни одна кнопка не должна «молча» ничего не делать: сбой — короткое объяснение пользователю."""
        upd, ex = ev.update, ev.exception
        chat = (upd.message.chat.id if upd.message else
                upd.callback_query.message.chat.id if upd.callback_query and upd.callback_query.message else None)
        net = isinstance(ex, TelegramNetworkError)
        log.warning("бот: %s при обработке %s", f"{type(ex).__name__}: {ex}"[:300], "кнопки" if upd.callback_query else "сообщения")
        if not net:
            log.debug("подробности", exc_info=ex)
            db.event("error", "bot", f"{type(ex).__name__}: {str(ex)[:200]}")
        if chat:
            try:
                await bot.send_message(chat, "⚠️ Связь с Telegram оборвалась на середине ответа — повторите, пожалуйста."
                                       if net else f"⚠️ Не получилось: {e(str(ex)[:200])}. Ошибка записана в журнал.")
            except Exception:  # noqa: BLE001 — если и это не ушло, пользователь увидит результат при следующем действии
                pass
        return True

    dp.include_router(r)
    ctx.bot = bot
    ctx.dp = dp                          # app.shutdown() штатно останавливает поллинг
    return bot, dp


async def run_bot(ctx: "Ctx", service: "Service") -> None:
    built = build_bot(ctx, service)
    if not built:
        return
    bot, dp = built
    delay = 5
    while True:   # Telegram из РФ может «моргать» — переподключаемся бесконечно
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in (
                ("menu", "Меню"), ("status", "Состояние Радара"), ("hot", "P1/P2 за сутки"), ("feed", "Лента"),
                ("scan", "Внеочередной сбор"), ("report", "Отчёт Excel+Word"), ("find", "Поиск по архиву"),
                ("replay", "Проверить текст"), ("sources", "Источники"), ("cand", "Кандидаты"),
                ("add", "Добавить по ссылке: канал, паблик, человек"), ("watch", "Люди на контроле"),
                ("brain", "Мозг (Colab)"), ("skipped", "Не присланные: чужие дома, копии"), ("help", "Все команды"))])
            await dp.start_polling(bot, handle_signals=False, polling_timeout=10)   # зависание заметим за ~30 с, а не 90
            return
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("бот: нет связи с Telegram (%s), повтор через %s с", ex, delay)
            await asyncio.sleep(delay)
            delay = min(300, delay * 2)
