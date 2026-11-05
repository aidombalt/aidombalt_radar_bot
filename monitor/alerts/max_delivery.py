"""MAX delivery adapter. Routing/priority/night decisions remain in Alerts."""
from __future__ import annotations
import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from ..util import iso, now_utc, ago, jdump, jload, html_escape as e
from .max_api import MaxAPI, MaxError, error_hint
from .render import Card, Btn, card_for

log = logging.getLogger("radar.max")


class MaxDelivery:
    def __init__(self, ctx):
        self.ctx = ctx
        self._api = None
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    @property
    def enabled(self):
        return self.ctx.settings.max.enabled and bool(self.ctx.settings.max_bot_token)

    @property
    def api(self):
        if self._api is None:
            s = self.ctx.settings
            self._api = MaxAPI(s.max_bot_token, s.max.api_url, s.max.ca_bundle)
        return self._api

    def target(self, recipient):
        return {"user_id": recipient[5:]} if str(recipient).startswith("user:") else {"chat_id": recipient}

    def enqueue_card(self, iid, recipient, priority, hold=None, update=False, escalation=0, fallback=False):
        if not self.enabled:
            return
        db, alerts = self.ctx.db, self.ctx.alerts
        it = db.item(iid)
        if not it or ((jload(it.get("analysis_json"), {}) or {}).get("_risk") or {}).get("copy_of"):
            return
        msg = db.one("SELECT * FROM max_msgs WHERE item_id=? AND chat_id=? AND kind='card'", (iid, recipient))
        if msg:
            if update or escalation:
                self.ping(iid, recipient, priority, update, escalation, msg["mid"])
                alerts.schedule_refresh(iid)
            return
        key = f"max:{iid}:{recipient}"
        old = db.one("SELECT * FROM outbox WHERE dedup_key=?", (key,))
        if old:
            if fallback and old["status"] == "pending":
                db.x("UPDATE outbox SET next_attempt_at=? WHERE id=?", (iso(), old["id"]))
            return
        body = {"kind": "card", "notify": not bool(hold), "fallback": fallback}
        alerts.enqueue("max", recipient, body, key, iid, priority, not_before=iso(hold) if hold else None)

    def enqueue_text(self, recipient, card, key, *, hold=None, priority="", reply_mid=None, files=None, public_control=False,
                     item_id=None):
        """item_id — сообщение относится к публикации (ответ «Черновик»/«Динамика», строка статуса): его mid
        запоминается и уходит из чата вместе с карточкой при уборке закрытых (alerts/cleanup.py)."""
        if not self.enabled:
            return
        body = card.max_body(notify=not bool(hold), reply_mid=reply_mid)
        payload = {"kind": "text", "body": body, "public_control": public_control}
        if files:
            payload["files"] = [str(x) for x in files]
        self.ctx.alerts.enqueue("max", recipient, payload, key, item_id=item_id, priority=priority,
                                not_before=iso(hold) if hold else None)

    def fallback(self, row):
        s = self.ctx.settings
        if not self.enabled or not s.max.fallback_from_tg or not s.max_admin_chat_id or not row["item_id"]:
            return
        if not self.ctx.alerts.still_relevant(row)[0]:
            return
        it = self.ctx.db.item(row["item_id"])
        if not it or it["priority"] not in ("P1", "P2"):
            return
        self.enqueue_card(it["id"], s.max_admin_chat_id, it["priority"], fallback=True)
        # Start independent delivery immediately; a blocked Telegram batch must not hold it up.
        try:
            asyncio.get_running_loop().create_task(self.process())
        except RuntimeError:
            pass

    def ping(self, iid, recipient, priority, update, escalation, mid):
        db = self.ctx.db
        db.x("UPDATE outbox SET status='dropped' WHERE channel='max' AND item_id=? AND recipient=? "
             "AND status='pending' AND dedup_key LIKE 'maxping:%'", (iid, recipient))
        what = (f"⏰ не взято в работу ({escalation}/{self.ctx.settings.alerts.max_escalations})" if 0 < escalation < 9 else
                "🔥 эскалировано вручную" if escalation == 9 else f"⬆️ повышено до {priority}")
        body = Card(f"<b>#{iid}</b> · {what} — карточка выше").max_body(reply_mid=mid)
        self.ctx.alerts.enqueue("max", recipient, {"kind": "ping", "body": body},
                                f"maxping:{iid}:{recipient}:{priority}:{int(update)}:{escalation}", iid, priority)

    async def delete_pings(self, iid, recipient=None):
        db = self.ctx.db
        rows = db.q("SELECT * FROM max_msgs WHERE item_id=? AND kind='ping'" + (" AND chat_id=?" if recipient else ""),
                    (iid, recipient) if recipient else (iid,))
        for row in rows:
            try:
                await self.api.delete(row["mid"])
            except MaxError as err:
                if err.temporary or err.denied:
                    raise
                if err.status not in (400, 404, 410):
                    raise
            db.x("DELETE FROM max_msgs WHERE mid=?", (row["mid"],))

    def pause(self, error, recipient=None):
        db = self.ctx.db
        # A revoked token pauses the whole channel; one unavailable DM pauses only that recipient.
        whole = error.status == 401 or recipient is None
        db.x("UPDATE outbox SET status='paused',last_error=? WHERE channel='max' AND status='pending'" +
             ("" if whole else " AND recipient=?"), (str(error),) if whole else (str(error), recipient))
        paused = db.kv_get("max_paused", {})
        paused["*" if whole else recipient] = str(error)
        db.kv_set("max_paused", paused)
        text = "🔴 " + str(error) + ": доставка приостановлена; проверьте токен, членство и права"
        db.kv_set("max_health", {"ok": False, "text": text, "checked_at": iso()})
        key = f"max-down:{error.status}:{recipient}:{now_utc():%Y%m%d}"
        # Do not route this warning back to the broken channel.
        self.ctx.alerts.admin_notice(text, key, include_max=False)
        users = db.q("SELECT email FROM users WHERE active=1 AND role='admin' AND email<>''")
        for u in users:
            self.ctx.alerts.enqueue("email", u["email"], {"to": [u["email"]], "subject": "Радар: Max недоступен", "html": f"<p>{e(text)}</p>"},
                                    f"maxfaultmail:{key}:{u['email']}")

    def resume(self, recipient):
        db = self.ctx.db
        paused = db.kv_get("max_paused", {})
        paused.pop("*", None); paused.pop(recipient, None)
        db.kv_set("max_paused", paused)
        # Token valid again; keep individually blocked DMs paused.
        for row in db.q("SELECT id,recipient FROM outbox WHERE channel='max' AND status='paused'"):
            if row["recipient"] not in paused:
                db.x("UPDATE outbox SET status='pending',next_attempt_at=? WHERE id=?", (iso(), row["id"]))

    async def process(self, limit=30):
        if not self.enabled:
            return 0
        async with self._lock:
            return await self._process(limit)

    async def _process(self, limit):
        db, count = self.ctx.db, 0
        rows = db.q("SELECT * FROM outbox WHERE channel='max' AND status='pending' AND next_attempt_at<=? "
                    "ORDER BY priority='P1' DESC,id LIMIT ?", (iso(), limit))
        for row in rows:
            # A callback or another delivery may have invalidated the selected row.
            live = db.one("SELECT status FROM outbox WHERE id=?", (row["id"],))
            if live["status"] != "pending":
                continue
            paused = db.kv_get("max_paused", {})
            if "*" in paused or row["recipient"] in paused:
                db.x("UPDATE outbox SET status='paused' WHERE id=?", (row["id"],)); continue
            ok, why = self.ctx.alerts.still_relevant(row)
            if not ok:
                db.x("UPDATE outbox SET status='dropped',last_error=? WHERE id=?", (why, row["id"])); continue
            payload = jload(row["payload_json"], {})
            if not payload.get("public_control"):
                recipient = row["recipient"]
                permitted = bool(db.one("SELECT 1 FROM users WHERE max_user_id=? AND active=1", (recipient[5:],))) \
                    if recipient.startswith("user:") else recipient == self.ctx.settings.max_admin_chat_id
                if not permitted:
                    db.x("UPDATE outbox SET status='dropped',last_error='адресат больше не разрешён' WHERE id=?", (row["id"],))
                    continue
            if row["dedup_key"].startswith("maxmanual:") and row["item_id"]:
                manual_item = db.item(row["item_id"])
                root = (((jload((manual_item or {}).get("analysis_json"), {}) or {}).get("_risk") or {}).get("copy_of"))
                if not manual_item or root:
                    db.x("UPDATE outbox SET status='dropped',last_error='копия или удалённая публикация' WHERE id=?", (row["id"],))
                    continue
            kind, iid = payload.get("kind", "text"), row["item_id"]
            try:
                if kind == "card":
                    if db.one("SELECT 1 FROM max_msgs WHERE item_id=? AND chat_id=? AND kind='card'", (iid, row["recipient"])):
                        db.x("UPDATE outbox SET status='sent',sent_at=? WHERE id=?", (iso(), row["id"])); continue
                    body = card_for(self.ctx, iid, links=True).max_body(notify=payload.get("notify", True))
                else:
                    body = payload["body"]
                if kind == "ping":
                    await self.delete_pings(iid, row["recipient"])
                    if not self.ctx.alerts.still_relevant(row)[0]:
                        db.x("UPDATE outbox SET status='dropped' WHERE id=?", (row["id"],)); continue
                if payload.get("files"):
                    uploaded = payload.setdefault("uploaded", [])
                    for path in payload["files"][len(uploaded):]:
                        from ..settings import DATA_DIR
                        target = Path(path).resolve()
                        if not target.is_relative_to((DATA_DIR / "reports").resolve()) or target.suffix not in (".xlsx", ".docx"):
                            raise MaxError(400, "report.path.denied")
                        uploaded.append(await self.api.upload(target))
                        db.x("UPDATE outbox SET payload_json=? WHERE id=?", (jdump(payload), row["id"]))
                    body = {**body, "attachments": [*body.get("attachments", []), *uploaded]}
                try:
                    response = await self.api.send(body, **self.target(row["recipient"]))
                except MaxError as err:
                    if body.get("link") and err.status in (400, 404, 410) and err.code in ("message.not.found", "message.deleted", "message.link.not.found"):
                        # Original card removed by hand. Status remains visible without a dangling reply.
                        body = {k: v for k, v in body.items() if k != "link"}
                        payload["body"] = body
                        db.x("UPDATE outbox SET payload_json=? WHERE id=?", (jdump(payload), row["id"]))
                        response = await self.api.send(body, **self.target(row["recipient"]))
                    else:
                        raise
                mid = ((response.get("message") or {}).get("body") or {}).get("mid")
                if not isinstance(mid, str) or not mid:
                    raise MaxError(0, "message.mid.missing")
                with db.tx():
                    if iid and kind in ("card", "ping"):
                        db.x("INSERT OR IGNORE INTO max_msgs(item_id,chat_id,mid,kind,sent_at,state) VALUES(?,?,?,?,?,?)",
                             (iid, row["recipient"], mid, kind, iso(), self.ctx.alerts.card_state(iid)[0]))
                    elif iid and kind == "text" and row["dedup_key"].startswith(("maxstatus:", "maxaction:")):
                        # строка статуса и ответы «Черновик»/«Динамика» к карточке — убираются вместе с ней
                        db.x("INSERT OR IGNORE INTO max_msgs(item_id,chat_id,mid,kind,sent_at,state,editable) "
                             "VALUES(?,?,?,?,?,?,0)", (iid, row["recipient"], mid,
                                                      "status" if row["dedup_key"].startswith("maxstatus:") else "reply",
                                                      iso(), ""))
                    db.x("UPDATE outbox SET status='sent',sent_at=?,attempts=attempts+1,last_error=NULL WHERE id=?", (iso(), row["id"]))
                count += 1
                if iid and kind == "ping" and not self.ctx.alerts.still_relevant(row)[0]:
                    await self.delete_pings(iid, row["recipient"])
                if iid and kind == "card":
                    # Re-read status after the network await; an operator could act during send.
                    self.ctx.alerts.queue_refresh(iid)
                    await self.sync_pin(iid)
            except MaxError as err:
                log.warning("Max: доставка сообщения: %s; %s", err, error_hint(err))
                if db.scalar("SELECT status FROM outbox WHERE id=?", (row["id"],)) == "sent":
                    if iid:
                        self.ctx.alerts.queue_refresh(iid, delay=15)
                    if err.denied:
                        self.pause(err, row["recipient"])
                    continue
                attempts = row["attempts"] + 1
                delay = err.retry_after or (1 if err.code == "attachment.not.ready" else min(600, 15 * 2 ** min(attempts-1, 6)))
                if err.denied:
                    self.pause(err, row["recipient"])
                    status = "paused"
                else:
                    status = "pending" if err.temporary else "dead"
                db.x("UPDATE outbox SET status=?,attempts=?,last_error=?,next_attempt_at=? WHERE id=?",
                     (status, attempts, str(err), iso(now_utc()+timedelta(seconds=delay)), row["id"]))
                if err.status == 429 or err.status == 401:
                    break
            except OSError:
                db.x("UPDATE outbox SET status='dead',last_error='файл отчёта недоступен' WHERE id=?", (row["id"],))
        return count

    async def refresh(self, iid, state=None, note=""):
        if not self.enabled:
            return 0
        async with self._refresh_lock:
            return await self._refresh(iid, state, note)

    async def _refresh(self, iid, state, note):
        db, n = self.ctx.db, 0
        state = state or self.ctx.alerts.card_state(iid)[0]
        for row in db.q("SELECT * FROM max_msgs WHERE item_id=? AND kind='card'", (iid,)):
            paused = db.kv_get("max_paused", {})
            if "*" in paused or row["chat_id"] in paused:
                raise MaxError(503, "delivery.paused")
            current = card_for(self.ctx, iid, state, note, links=True).max_body(notify=False)
            if row["editable"]:
                try:
                    await self.api.edit(row["mid"], current)
                    db.x("UPDATE max_msgs SET state=? WHERE mid=?", (state, row["mid"]))
                    n += 1
                except MaxError as err:
                    if err.denied:
                        self.pause(err, row["chat_id"]); raise
                    if err.temporary:
                        raise
                    if err.status not in (400, 403, 404, 410):
                        raise
                    db.x("UPDATE max_msgs SET editable=0 WHERE mid=?", (row["mid"],))
            editable = db.scalar("SELECT editable FROM max_msgs WHERE mid=?", (row["mid"],))
            if not editable and state != row["state"]:
                labels = {"resolved": "🟢 Решено", "false_positive": "❌ Ложное", "in_work": "🛠 В работе",
                          "open": "↩️ Возвращено", "escalated": "🔥 Эскалировано", "dropped": "⚪ Снято"}
                who = self.ctx.alerts.card_state(iid)[1]
                fb = db.scalar("SELECT MAX(id) FROM feedback WHERE item_id=?", (iid,), 0)
                self.enqueue_text(row["chat_id"], Card(f"#{iid} · {labels.get(state, state)} · {e(who)}"),
                                  f"maxstatus:{row['mid']}:{state}:{fb}", reply_mid=row["mid"], item_id=iid)
                db.x("UPDATE max_msgs SET state=? WHERE mid=?", (state, row["mid"]))
        if state not in ("open", "escalated"):
            await self.delete_pings(iid)
        await self.sync_pin(iid)
        return n

    async def delete_cards(self, iid):
        """Убрать из Max карточку публикации и всё, что к ней относится (пинги, статусы, ответы). Возвращает
        (удалено, всё ли разобрано). Ответ Max «нет сообщения / нельзя» — окончательный: строку забываем.
        Сеть, 429, приостановленный канал — повтор позже (alerts/cleanup.py считает попытки)."""
        if not self.enabled:
            return 0, True
        db, n = self.ctx.db, 0
        paused = db.kv_get("max_paused", {}) or {}
        for row in db.q("SELECT * FROM max_msgs WHERE item_id=? ORDER BY kind<>'card'", (iid,)):
            if "*" in paused or row["chat_id"] in paused:
                return n, False
            try:
                if row["pinned"] and not row["chat_id"].startswith("user:"):
                    try:
                        await self.api.unpin(row["chat_id"])
                    except MaxError as err:
                        if err.temporary:
                            raise
                await self.api.delete(row["mid"])
                n += 1
            except MaxError as err:
                if err.temporary or err.denied:
                    log.warning("Max: карточка #%s — удаление отложено: %s; %s", iid, err, error_hint(err))
                    return n, False
                log.info("Max: сообщение карточки #%s не удалено (%s) — оставлено как есть", iid, err)
            db.x("DELETE FROM max_msgs WHERE mid=?", (row["mid"],))
        return n, True

    async def sync_pin(self, iid):
        if not self.enabled:
            return
        db = self.ctx.db
        for row in db.q("SELECT * FROM max_msgs WHERE item_id=? AND kind='card'", (iid,)):
            if row["chat_id"].startswith("user:"):
                continue
            it = db.item(iid) or {}
            should = self.ctx.settings.max.pin_p1 and it.get("priority") == "P1" and it.get("workflow") == "open"
            try:
                if should and not row["pinned"]:
                    await self.api.pin(row["chat_id"], row["mid"])
                    db.x("UPDATE max_msgs SET pinned=0 WHERE chat_id=?", (row["chat_id"],))
                    db.x("UPDATE max_msgs SET pinned=1 WHERE mid=?", (row["mid"],))
                elif not should and row["pinned"]:
                    current = await self.api.pinned(row["chat_id"])
                    message = current.get("message") or {}
                    if (message.get("body") or {}).get("mid") == row["mid"]:
                        await self.api.unpin(row["chat_id"])
                    db.x("UPDATE max_msgs SET pinned=0 WHERE mid=?", (row["mid"],))
            except MaxError as err:
                # Pinning is optional and must never make a delivered card retry as a new message.
                self.ctx.db.event("warning", "max", f"закреп #{iid}: {err}")

    async def test(self):
        if not self.enabled or not self.ctx.settings.max_admin_chat_id:
            raise ValueError("Задайте MAX_BOT_TOKEN и MAX_ADMIN_CHAT_ID")
        import secrets
        nonce = secrets.token_urlsafe(12)
        self.ctx.db.kv_set("max_test", {"nonce": nonce, "at": iso()})
        self.enqueue_text(self.ctx.settings.max_admin_chat_id, Card("✅ Тест Max. Нажмите кнопку для проверки обратного канала.",
                          [[Btn("Подтвердить", f"test:{nonce}")]]), f"maxtest:{nonce}")
        await self.process()
