"""One reusable Telegram panel per user/chat; alert messages remain independent."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..util import ago, jload
from . import commands
from .render import Btn, Card


class TelegramMenu:
    def __init__(self, ctx, bot, menu):
        self.ctx, self.bot, self.menu = ctx, bot, menu
        self._locks = {}

    def state_key(self, chat_id, mid):
        return f"tg_menu:{chat_id}:{mid}"

    def active_key(self, chat_id, uid):
        return f"tg_menu_active:{chat_id}:{uid}"

    def state(self, chat_id, mid):
        s = self.ctx.db.kv_get(self.state_key(chat_id, mid), {})
        return s if s.get("expires", 0) > time.time() else {}

    def root_markup(self, role):
        return InlineKeyboardMarkup(inline_keyboard=[
            [b for b in row if role in ("manager", "admin") or b.callback_data not in ("m:scan", "m:report")]
            for row in self.menu.inline_keyboard if any(role in ("manager", "admin") or b.callback_data not in ("m:scan", "m:report") for b in row)])

    def back(self, action="home"):
        return [Btn("⬅️ Назад", "m:"+action), Btn("🏠 Меню", "m:home")]

    async def edit_raw(self, chat_id, mid, card, markup=None):
        kb = markup or InlineKeyboardMarkup.model_validate(card.telegram()["reply_markup"])
        try:
            await self.bot.edit_message_text(card.text_html, chat_id=chat_id, message_id=mid,
                                             reply_markup=kb, disable_web_page_preview=True)
            return mid
        except TelegramBadRequest as err:
            text = err.message.lower()
            if "message is not modified" in text:
                return mid
            if not any(x in text for x in ("message to edit not found", "message can't be edited",
                                           "message can not be edited", "message_id_invalid")):
                raise
            # A deleted/noneditable panel can be replaced once. Network errors never create a copy.
            msg = await self.bot.send_message(chat_id, card.text_html, reply_markup=kb, disable_web_page_preview=True)
            return msg.message_id

    async def show(self, chat_id, uid, cards, *, mid=None, page=0, back="home", root=False, role="viewer", screen="",
                   parent=None, force_new=False, bind_active=True):
        key = self.active_key(chat_id, uid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if mid is None and not force_new:
                active = self.ctx.db.kv_get(key, {})
                mid = active.get("mid") if self.state(chat_id, active.get("mid")) else None
            previous = self.state(chat_id, mid) if mid is not None else {}
            if previous and previous["owner"] != str(uid):
                return None
            page = min(max(0, page), max(0, len(cards)-1))
            card = Card(cards[page].text_html, [list(row) for row in cards[page].rows])
            if not root:
                nav = []
                if page:
                    nav.append(Btn("◀️ Предыдущие", f"m:page:{page-1}"))
                if page+1 < len(cards):
                    nav.append(Btn("Следующие ▶️", f"m:page:{page+1}"))
                if nav:
                    card.rows.append(nav)
                    card.text_html += f"\n\nСтраница {page+1} из {len(cards)}"
                card.rows.append(self.back(back))
            markup = self.root_markup(role) if root else None
            if mid is None:
                msg = await self.bot.send_message(chat_id, card.text_html, reply_markup=markup or InlineKeyboardMarkup.model_validate(card.telegram()["reply_markup"]),
                                                 disable_web_page_preview=True)
                mid = msg.message_id
            else:
                mid = await self.edit_raw(chat_id, mid, card, markup)
            state = {"owner": str(uid), "expires": time.time()+86400, "pages": [asdict(c) for c in cards],
                     "page": page, "back": back, "revision": previous.get("revision", 0)+1, "screen": screen,
                     "parent": parent if parent is not None else previous.get("parent")}
            self.ctx.db.kv_set(self.state_key(chat_id, mid), state)
            if bind_active:
                self.ctx.db.kv_set(key, {"mid": mid})
            return mid, state["revision"]

    async def publish(self, message, role, cards, **kwargs):
        return await self.show(message.chat.id, message.from_user.id, cards, role=role, **kwargs)

    async def callback(self, c, role, cards, **kwargs):
        return await self.show(c.message.chat.id, c.from_user.id, cards, mid=c.message.message_id, role=role, **kwargs)

    async def page(self, c, role, number):
        s = self.state(c.message.chat.id, c.message.message_id)
        if not s or s["owner"] != str(c.from_user.id):
            return False
        cards = [Card(p["text_html"], [[Btn(**b) for b in row] for row in p["rows"]]) for p in s["pages"]]
        await self.callback(c, role, cards, page=number, back=s["back"], screen=s["screen"])
        return True

    async def parent(self, c, role):
        s = self.state(c.message.chat.id, c.message.message_id)
        p = s.get("parent")
        if not p:
            return False
        cards = [Card(x["text_html"], [[Btn(**b) for b in row] for row in x["rows"]]) for x in p["pages"]]
        await self.callback(c, role, cards, page=p["page"], screen=p["screen"])
        return True

    def items(self, name, args=""):
        db = self.ctx.db
        if name == "find":
            from ..textnorm import lemma_string
            rows = db.fts_search(lemma_string(args), 100)
        elif name == "hot":
            rows = db.q("SELECT * FROM items WHERE analyzed_at>=? AND priority IN ('P1','P2') ORDER BY analyzed_at DESC,id DESC LIMIT 100", (ago(hours=24),))
        else:
            n = min(100, max(1, int(args))) if args.isdigit() else 30
            rows = db.q("SELECT * FROM items WHERE status='analyzed' AND relevant=1 ORDER BY analyzed_at DESC,id DESC LIMIT ?", (n,))
        title = {"hot": "🔥 Горячее за сутки", "feed": "📋 Лента публикаций", "find": "🔎 Результаты поиска"}[name]
        if not rows:
            return [Card(f"<b>{title}</b>\n"+("За сутки P1/P2 нет ✅" if name == "hot" else "Пусто."))]
        cards = []
        for start in range(0, len(rows), 5):
            page = rows[start:start+5]
            text = f"<b>{title}</b>\n\n" + "\n\n".join(commands.item_short(self.ctx, it) for it in page)
            cards.append(Card(text, [[Btn(f"Открыть #{it['id']}", f"m:item:{it['id']}")] for it in page]))
        return cards

    async def item(self, c, role, iid, state=None):
        actor = commands.Actor(str(c.from_user.id), c.from_user.full_name, role, "Telegram")
        cards = await commands.command(self.ctx, "item", str(iid), actor, None, c.message.chat.type == "private")
        if cards[0].item_id:
            cards = [commands.card_for(self.ctx, iid, state)]
        if role not in ("manager", "admin"):
            cards[0].rows = [[b for b in row if b.payload is None or b.payload.startswith(("a:dyn:", "a:rep:", "a:card:"))]
                            for row in cards[0].rows]
        s = self.state(c.message.chat.id, c.message.message_id)
        parent = s.get("parent") if s.get("screen", "").startswith(("item:", "detail:")) else \
            {k:s[k] for k in ("pages", "page", "screen")} if s.get("screen") in ("hot", "feed", "find") else None
        await self.callback(c, role, cards, back="parent" if parent else "home", screen=f"item:{iid}", parent=parent)

    def cleanup(self):
        self.ctx.db.x("DELETE FROM kv WHERE key LIKE 'tg_menu:%' AND json_extract(value,'$.expires')<?", (time.time(),))
