"""MAX events: durable inbox, polling/webhook, access control and health checks."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, HTTPException

from ..util import iso, now_utc, ago, jdump, jload, html_escape as e
from .max_api import MaxError, error_hint
from .render import Card, Btn, card_for
from .commands import Actor, handle_action, command, redeem_link, ADMIN_COMMANDS
from .max_menu import MaxMenu, ENTRY_COMMANDS

UPDATE_TYPES = ["message_created", "message_callback", "bot_added", "bot_removed", "bot_admin_permissions_changed", "bot_started"]
log = logging.getLogger("radar.max")
COMMANDS = [{"name": name, "description": label} for name, label in (
    ("menu", "Меню с кнопками"), ("feed", "Лента публикаций"), ("find", "Поиск в архиве"),
    ("sources", "Источники"), ("cand", "Кандидаты в источники"), ("settings", "Отправки в группу (админ)"),
    ("status", "Состояние Радара"), ("hot", "P1/P2 за сутки"), ("item", "Карточка по номеру"),
    ("skipped", "Повторы и отсеянные публикации"), ("report", "Отчёт"), ("scan", "Запустить сбор"),
    ("brain", "Состояние ИИ"), ("chatid", "ID этой группы"), ("link", "Привязать аккаунт"), ("help", "Команды"))]


def event_key(event):
    kind = event.get("update_type")
    if kind == "message_callback":
        cb = event.get("callback")
        if not isinstance(cb, dict) or not isinstance(cb.get("callback_id"), str) or not cb["callback_id"]:
            raise ValueError("invalid.callback")
        return "callback:" + cb["callback_id"]
    if kind == "message_created":
        message = event.get("message") or {}
        mid = (message.get("body") or {}).get("mid")
        if not mid:
            raise ValueError("invalid.message")
        return "message:" + str(mid)
    return "event:" + hashlib.sha256(jdump(event).encode()).hexdigest()


class MaxBot:
    def __init__(self, ctx, service=None):
        self.ctx, self.service = ctx, service
        self.delivery = ctx.alerts.max
        self._drain_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()
        self._last_poll = 0.0
        self._transport = None
        self.menu = MaxMenu(self)

    @property
    def api(self):
        return self.delivery.api

    def actor(self, user):
        uid = str((user or {}).get("user_id", ""))
        row = self.ctx.db.one("SELECT * FROM users WHERE max_user_id=? AND active=1", (uid,))
        return Actor(uid, (row["name"] or uid) if row else uid, row["role"] if row else "", "Max")

    async def welcome(self, actor, key):
        if not self.ctx.settings.max.direct_messages or not actor.user_id.isdigit():
            return
        if actor.role:
            _, result = await self.menu.open(actor, "user:" + actor.user_id, True)
            card = result.card
            card.text_html = "Радар подключён. /menu — открыть меню.\n\n" + card.text_html
        else:
            text = ("Радар на связи. Для доступа нужно привязать аккаунт.\n"
                    "1. Отправьте /link в личке Telegram-бота Радара.\n"
                    "2. Пришлите полученную команду /link КОД сюда (код действует 10 минут).\n"
                    f"Или передайте администратору ваш Max ID: <code>{e(actor.user_id)}</code>.")
            card = Card(text)
        self.delivery.enqueue_text("user:" + actor.user_id, card, "maxstart:" + key,
                                   public_control=not bool(actor.role))

    def ingest(self, event):
        if not isinstance(event, dict) or event.get("update_type") not in UPDATE_TYPES:
            return None  # unrelated events are acknowledged, never interpreted as commands
        key = event_key(event)
        self.ctx.db.x("INSERT OR IGNORE INTO max_events(event_key,payload_json,created_at,next_attempt_at) VALUES(?,?,?,?)",
                      (key, jdump(event), iso(), iso()))
        return key

    async def handle(self, event, key=None):
        # Single event mutation stream, shared with polling and webhook.
        async with self._event_lock:
            await self._handle(event, key or event_key(event))

    async def _handle(self, event, key):
        db = self.ctx.db
        kind = event["update_type"]
        if kind in ("bot_added", "bot_removed", "bot_admin_permissions_changed"):
            chat = str(event.get("chat_id", ""))
            if kind == "bot_added":
                db.kv_set(f"max_seen_chat:{chat}", {"chat_id": chat, "at": iso()})
                self.ctx.alerts.admin_notice(f"Max: бота добавили в группу {e(chat)}. Разрешённая группа задаётся в MAX_ADMIN_CHAT_ID.",
                                             f"max-added:{key}")
            if chat != self.ctx.settings.max_admin_chat_id:
                return
            if kind == "bot_removed":
                self.delivery.pause(MaxError(403, "chat.denied"), chat)
            else:
                await self.check_health()
            return
        if kind == "bot_started":
            actor = self.actor(event.get("user"))
            await self.welcome(actor, key)
            return
        if kind == "message_callback":
            await self.callback(event, key)
            return
        message = event.get("message") or {}
        sender = message.get("sender") or {}
        if sender.get("is_bot"):
            return
        text = ((message.get("body") or {}).get("text") or "").strip()
        if not text.startswith("/"):
            recipient = message.get("recipient") or {}
            private = recipient.get("chat_type") == "dialog"
            chat = str(recipient.get("chat_id", ""))
            actor = self.actor(sender)
            if not actor.role or (private and not self.ctx.settings.max.direct_messages) or (not private and chat != self.ctx.settings.max_admin_chat_id):
                return
            destination = "user:" + actor.user_id if private else chat
            result = await self.menu.consume_text(actor, destination, private, text, key,
                                                  event.get("timestamp") or message.get("timestamp"))
            if result:
                await self.send_menu_result(result, actor, destination, private, key)
            return
        name, _, args = text.partition(" ")
        name = name[1:].split("@", 1)[0].lower(); args = args.strip()
        recipient = message.get("recipient") or {}
        private = recipient.get("chat_type") == "dialog"
        chat = str(recipient.get("chat_id", ""))
        actor = self.actor(sender)
        destination = "user:" + actor.user_id if private else chat
        mid = (message.get("body") or {}).get("mid")
        if name == "chatid":
            if chat:
                self.delivery.enqueue_text(destination, Card(f"ID чата: <code>{e(chat)}</code>\nВаш Max ID: <code>{e(actor.user_id)}</code>"), "maxreply:"+key, public_control=True)
            return
        if private:
            if not self.ctx.settings.max.direct_messages:
                return
        elif chat != self.ctx.settings.max_admin_chat_id:
            return
        if name == "start" and private:
            await self.welcome(actor, key)
            return
        if name == "link":
            if not private:
                self.delivery.enqueue_text(chat, Card("Введите код в личке Max-бота; новый код получите через /link в личке Telegram."), "maxreply:"+key)
                return
            # Limit guessed codes without retaining their plaintext.
            rate_key = f"max_link_attempt:{actor.user_id}"
            rate = db.kv_get(rate_key, {"start": iso(), "n": 0})
            if rate["start"] < ago(minutes=10):
                rate = {"start": iso(), "n": 0}
            rate["n"] += 1; db.kv_set(rate_key, rate)
            text = "Слишком много попыток. Повторите через 10 минут" if rate["n"] > 5 else redeem_link(self.ctx, args, actor.user_id)
            linked = self.actor(sender)
            card = Card(e(text))
            if linked.role:
                _, result = await self.menu.open(linked, destination, True)
                card.rows = result.card.rows
            self.delivery.enqueue_text(destination, card, "maxreply:"+key, public_control=True)
            return
        if not actor.role:
            self.delivery.enqueue_text(destination, Card(f"Нет доступа, ваш id {e(actor.user_id)} — передайте администратору"), "maxreply:"+key, public_control=True)
            return
        timestamp = event.get("timestamp") or message.get("timestamp")
        # Missing/invalid timestamps fail closed for side-effect commands.
        side_effect = name in ("scan", "report") or name in ADMIN_COMMANDS
        stale = not isinstance(timestamp, (int, float)) or time.time()*1000 - timestamp > 10*60*1000 or timestamp > time.time()*1000+60000
        if name in ENTRY_COMMANDS and not (side_effect and stale):
            _, result = await self.menu.open(actor, destination, private, name, args)
            await self.send_menu_result(result, actor, destination, private, key)
            self.menu.kick_reports()
            return
        if side_effect and stale:
            cards = [Card("Команда устарела, повторите")]
        else:
            cards = await command(self.ctx, name, args, actor, self.service, private)
        for i, card in enumerate(cards):
            if card.item_id:
                await self.show_card(card.item_id, destination)
            else:
                files = card.files if self.ctx.settings.max.report_files else []
                if card.files and not files:
                    card.text_html += "\n📎 Excel и Word доступны в Радаре → Отчёты. Отправка файлов в Max выключена."
                self.delivery.enqueue_text(destination, card, f"maxreply:{key}:{i}", reply_mid=mid, files=files)

    async def send_menu_result(self, result, actor, destination, private, key):
        if result.card:
            self.delivery.enqueue_text(destination, result.card, "maxmenu:"+key)
        elif result.notification:
            self.delivery.enqueue_text(destination, Card(e(result.notification)), "maxmenu:"+key)
        if result.item_id:
            cards = await command(self.ctx, "item", str(result.item_id), actor, self.service, private)
            if cards[0].item_id:
                await self.show_card(result.item_id, destination)
            else:
                self.delivery.enqueue_text(destination, cards[0], "maxmenuitem:"+key)

    async def show_card(self, iid, destination):
        db = self.ctx.db
        it = db.item(iid) or {}
        root = ((jload(it.get("analysis_json"), {}) or {}).get("_risk") or {}).get("copy_of")
        if root and db.item(int(root)):
            iid = int(root)
        existing = db.one("SELECT mid FROM max_msgs WHERE item_id=? AND chat_id=? AND kind='card'", (iid, destination))
        if existing:
            self.ctx.alerts.queue_refresh(iid)
            await self.ctx.alerts.flush_refresh()
            return
        # Explicit operator request may open a P3/P4 card; it must not be dropped as an automatic alert.
        payload = {"kind": "card", "notify": False}
        self.ctx.alerts.enqueue("max", destination, payload, f"maxmanual:{iid}:{destination}", iid)
        await self.delivery.process()

    async def callback(self, event, key):
        db = self.ctx.db
        callback = event.get("callback") or {}
        actor = self.actor(callback.get("user"))
        cid = callback["callback_id"]
        message = event.get("message")
        payload = callback.get("payload") or ""
        if not actor.role:
            await self.api.answer(cid, f"Нет доступа, ваш id {actor.user_id} — передайте администратору")
            return
        if not message:
            # No trusted chat identity; never let a forwarded/deleted button mutate the database.
            await self.api.answer(cid, "Карточка удалена. Откройте её через /item в разрешённом чате")
            return
        recipient = message.get("recipient") or {}
        private = recipient.get("chat_type") == "dialog"
        chat = str(recipient.get("chat_id", ""))
        if (private and not self.ctx.settings.max.direct_messages) or (not private and chat != self.ctx.settings.max_admin_chat_id):
            await self.api.answer(cid, "Этот чат не подключён к Радару")
            return
        destination = "user:" + actor.user_id if private else chat
        mid = (message.get("body") or {}).get("mid")
        if payload == "menu:open":
            _, result = await self.menu.open(actor, destination, private)
            await self.send_menu_result(result, actor, destination, private, key)
            try:
                await self.api.answer(cid, "Открываю меню")
            except MaxError as err:
                if err.temporary or err.denied:
                    raise
            return
        if payload.startswith("m:"):
            result = await self.menu.callback(payload, actor, destination, private, key,
                                              callback.get("timestamp") or event.get("timestamp"))
            self.menu.kick_reports()
            try:
                await self.api.answer(cid, result.notification, result.card.max_body(False) if result.card else None)
            except MaxError as err:
                if err.temporary or err.denied:
                    raise
                await self.send_menu_result(result, actor, destination, private, key)
                return
            if result.item_id:
                await self.send_menu_result(type(result)(item_id=result.item_id), actor, destination, private, key)
            return
        if payload.startswith("test:"):
            test = db.kv_get("max_test", {})
            if payload[5:] == test.get("nonce") and test.get("at", "") > ago(minutes=30):
                db.kv_set("max_test_ok", {"by": actor.name, "at": iso()})
                await self.api.answer(cid, "✅ Обратный канал работает", Card("✅ Max: доставка и нажатие проверены").max_body(False))
            else:
                await self.api.answer(cid, "Тест устарел — запустите новый в GUI")
            return
        result = await handle_action(self.ctx, payload, actor, key, self.service)
        body = None
        if result.item_id and not result.reply_html and not result.choices and not payload.startswith("a:card:"):
            record = db.one("SELECT editable FROM max_msgs WHERE mid=? AND item_id=? AND kind='card'", (mid, result.item_id))
            if record and record["editable"]:
                body = card_for(self.ctx, result.item_id, result.state, links=True).max_body(False)
        try:
            await self.api.answer(cid, result.notification, body)
        except MaxError as err:
            if err.temporary or err.denied:
                raise
            if body is not None:
                await self.api.answer(cid, result.notification)
            # Expired callback answer is harmless: action is already durable; refresh uses /messages.
        if result.reply_html:
            self.delivery.enqueue_text(destination, Card(result.reply_html, [[Btn("🏠 Меню", "menu:open")]]),
                                       "maxaction:"+key, reply_mid=mid, item_id=result.item_id)
        elif result.choices:
            rows = [[Btn(self.ctx.reg.company_name(k), f"h:our:{result.item_id}:{k}")] for k in result.choices]
            self.delivery.enqueue_text(destination, Card(result.notification, rows), "maxaction:"+key, reply_mid=mid)
        elif payload.startswith("a:card:") and result.item_id:
            await self.show_card(result.item_id, destination)
        if result.item_id:
            await self.ctx.alerts.flush_refresh()

    async def drain(self, only=None):
        async with self._drain_lock:
            db = self.ctx.db
            if only:
                rows = db.q("SELECT * FROM max_events WHERE event_key=? AND status='pending'", (only,))
            else:
                rows = db.q("SELECT * FROM max_events WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT 30", (iso(),))
            for row in rows:
                try:
                    await self.handle(jload(row["payload_json"], {}), row["event_key"])
                    db.x("UPDATE max_events SET status='done',done_at=?,payload_json='{}' WHERE event_key=?", (iso(), row["event_key"]))
                except MaxError as err:
                    log.warning("Max: обработка события: %s; %s", err, error_hint(err))
                    if err.denied:
                        self.delivery.pause(err, None)
                    delay = err.retry_after or min(600, 15*2**min(row["attempts"], 6))
                    db.x("UPDATE max_events SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE event_key=?",
                         (iso(now_utc()+timedelta(seconds=delay)), str(err), row["event_key"]))
                    if only:
                        return False
                except Exception as err:
                    # Poison events do not crash the event loop or leak user data/tokens to logs.
                    log.error("Max: событие не обработано (%s)", type(err).__name__)
                    db.x("UPDATE max_events SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE event_key=?",
                         (iso(now_utc()+timedelta(seconds=60)), type(err).__name__, row["event_key"]))
                    if row["attempts"] >= 4:
                        db.x("UPDATE max_events SET status='dead' WHERE event_key=?", (row["event_key"],))
                        self.ctx.alerts.admin_notice("Max: одно событие не обработано. Проверьте журнал и повторите команду.", "max-event:"+row["event_key"])
                    if only:
                        return False
            return True

    async def poll_once(self):
        await asyncio.sleep(max(0, .3 - (time.monotonic() - self._last_poll)))
        self._last_poll = time.monotonic()
        db = self.ctx.db
        data = await self.api.updates(db.kv_get("max_marker"), self.ctx.settings.max.poll_timeout, UPDATE_TYPES)
        if data.get("updates"):
            log.info("Max: получено событий: %s", len(data["updates"]))
        for event in data.get("updates", []):
            try:
                key = self.ingest(event)
            except ValueError:
                db.event("warning", "max", "MAX прислал событие без обязательных полей")
                continue
            if key and not await self.drain(key):
                return False
        if data.get("marker") is not None:
            db.kv_set("max_marker", data["marker"])
        return True

    async def check_health(self):
        if not self.delivery.enabled:
            return False
        db, s = self.ctx.db, self.ctx.settings
        stage = "проверка токена (/me)"
        try:
            bot = await self.api.me()
            db.kv_set("max_bot_id", str(bot.get("user_id", "")))
            # Token recovery must not depend on a configured/accessible admin group.
            self.delivery.resume(None)
            log.info("Max: токен принят")
            if not s.max_admin_chat_id:
                text, ok = "🟠 MAX_ADMIN_CHAT_ID не задан; /chatid в группе покажет ID", False
            else:
                stage = "проверка админ-группы"
                chat = await self.api.chat(s.max_admin_chat_id)
                member = await self.api.member(s.max_admin_chat_id)
                permissions = set(member.get("permissions") or [])
                missing = []
                if not (member.get("is_admin") or member.get("is_owner")):
                    missing.append("администратор")
                if not member.get("is_owner"):
                    if "read_all_messages" not in permissions:
                        missing.append("read_all_messages (кнопки работают, команды в группе — нет)")
                    if not permissions & {"write", "post_edit_delete_message"}:
                        missing.append("write")
                    if s.max.pin_p1 and "pin_message" not in permissions:
                        missing.append("pin_message")
                title = str(chat.get("title") or s.max_admin_chat_id)[:120]
                text = f"{'🟠' if missing else '🟢'} админ-группа «{title}»: " + ("нет прав: " + ", ".join(missing) if missing else "права в порядке")
                ok = not missing
                # Without write rights, pause delivery instead of repeatedly failing.
                if "write" in missing:
                    self.delivery.pause(MaxError(403, "chat.denied"), s.max_admin_chat_id)
                else:
                    self.delivery.resume(s.max_admin_chat_id)
            old = db.kv_get("max_health", {})
            db.kv_set("max_health", {"ok": ok, "text": text, "checked_at": iso()})
            if ok:
                log.info("Max: права в админ-группе в порядке")
            elif not s.max_admin_chat_id:
                log.warning("Max: MAX_ADMIN_CHAT_ID не задан; личные команды доступны, /chatid в группе покажет ID")
            else:
                log.warning("Max: в админ-группе нет прав: %s", ", ".join(missing))
            if not ok and old.get("text") != text:
                self.ctx.alerts.admin_notice("Max: " + e(text), "max-health:" + hashlib.sha256(text.encode()).hexdigest()[:16], include_max=False)
            return ok
        except MaxError as err:
            log.warning("Max: %s: %s; %s", stage, err, error_hint(err))
            if err.denied:
                self.delivery.pause(err, s.max_admin_chat_id)
            else:
                db.kv_set("max_health", {"ok": False, "text": "🔴 " + str(err), "checked_at": iso()})
            return False

    def webhook_url(self):
        s = self.ctx.settings
        url = s.max.webhook_url or (s.public_url.rstrip("/") + "/api/max/webhook" if s.public_url else "")
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.port not in (None, 443) or p.username or p.password or p.path != "/api/max/webhook" or p.query or p.fragment:
            raise ValueError("Max webhook требует публичный HTTPS:443 с путём /api/max/webhook")
        if not 16 <= len(s.max_webhook_secret) <= 256 or not all(c.isalnum() or c in "_-" for c in s.max_webhook_secret):
            raise ValueError("MAX_WEBHOOK_SECRET: 16–256 букв, цифр, _ или -")
        return url

    async def configure_transport(self, force=False):
        s, db = self.ctx.settings, self.ctx.db
        mode = s.max.mode
        subscriptions = (await self.api.subscriptions()).get("subscriptions", [])
        if mode == "polling":
            for sub in subscriptions:
                await self.api.unsubscribe(sub["url"])
        else:
            url = self.webhook_url()
            signature = hashlib.sha256((url + s.max_webhook_secret + jdump(UPDATE_TYPES)).encode()).hexdigest()
            for sub in subscriptions:
                if sub["url"] != url:
                    await self.api.unsubscribe(sub["url"])
            found = next((sub for sub in subscriptions if sub["url"] == url), None)
            if force or not found or db.kv_get("max_webhook_signature") != signature or set(found.get("update_types") or []) != set(UPDATE_TYPES):
                await self.api.subscribe(url, s.max_webhook_secret, UPDATE_TYPES)
                db.kv_set("max_webhook_signature", signature)
        self._transport = mode

    async def run(self):
        if not self.delivery.enabled:
            log.warning("Max: не запущен — %s", "max.enabled=false" if not self.ctx.settings.max.enabled
                        else "MAX_BOT_TOKEN не загружен; проверьте файл .env рядом с run.py")
            return
        log.info("Max: запуск; режим=%s; токен загружен; личные сообщения=%s",
                 self.ctx.settings.max.mode, "включены" if self.ctx.settings.max.direct_messages else "выключены")
        health_at, subscription_at, commands_at, ready, receiving = 0.0, 0.0, 0.0, False, False
        while True:
            stage = "проверка состояния"
            try:
                current = time.monotonic()
                if current >= health_at:
                    await self.check_health()
                    healthy = self.ctx.db.kv_get("max_health", {}).get("ok")
                    health_at = current + (86400 if healthy else 300)
                if not ready or self._transport != self.ctx.settings.max.mode or current >= subscription_at:
                    stage = "настройка получения событий"
                    await self.configure_transport(force=not ready)
                    ready = True; subscription_at = current + 3600
                    log.info("Max: получение событий настроено (%s)", self._transport)
                if current >= commands_at:
                    # Autocomplete is optional: a menu failure must never stop /start or /link.
                    commands_at = current + 300
                    try:
                        await self.api.set_commands(COMMANDS)
                        commands_at = current + 86400
                    except MaxError as err:
                        log.warning("Max: меню команд не обновлено: %s; приём команд продолжается", err)
                    except Exception as err:
                        log.warning("Max: меню команд не обновлено (%s); приём команд продолжается", type(err).__name__)
                stage = "обработка событий"
                await self.drain()
                self.menu.kick_reports()
                self.menu.cleanup()
                if self.ctx.settings.max.mode == "polling":
                    stage = "получение событий (/updates)"
                    if not await self.poll_once():
                        await asyncio.sleep(2)
                    elif not receiving:
                        receiving = True
                        log.info("Max: polling работает, команды принимаются")
                else:
                    await asyncio.sleep(.5)
                self.ctx.db.x("DELETE FROM max_events WHERE status='done' AND done_at<?", (ago(hours=24),))
                self.ctx.db.x("DELETE FROM action_receipts WHERE created_at<?", (ago(hours=24),))
            except asyncio.CancelledError:
                await self.menu.close()
                raise
            except MaxError as err:
                log.warning("Max: %s: %s; %s", stage, err, error_hint(err))
                self.ctx.db.kv_set("max_health", {"ok": False, "text": f"🔴 {stage}: {err}; {error_hint(err)}", "checked_at": iso()})
                if err.denied:
                    self.delivery.pause(err)
                await asyncio.sleep(min(60, err.retry_after or 15))
            except Exception as err:
                log.error("Max: %s: %s; проверьте config/settings.yaml (max) и путь к сертификату max.ca_bundle",
                          stage, type(err).__name__)
                self.ctx.db.kv_set("max_health", {"ok": False, "text": "🔴 ошибка настройки Max: " + type(err).__name__, "checked_at": iso()})
                await asyncio.sleep(15)


def make_router(ctx):
    router = APIRouter()

    @router.post("/api/max/webhook")
    async def webhook(request: Request):
        s = ctx.settings
        if not s.max.enabled or not s.max_bot_token or s.max.mode != "webhook" or not s.max_webhook_secret:
            raise HTTPException(503, "Max webhook disabled")
        secret = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(secret.encode(), s.max_webhook_secret.encode()):
            raise HTTPException(403, "Forbidden")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 256_000:
                raise HTTPException(413, "Event too large")
        try:
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError
            ctx.max_bot.ingest(event)  # 200 only after durable SQLite INSERT, commands processed by worker
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid event") from None
        return {"ok": True}
    return router
