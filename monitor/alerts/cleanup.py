"""Уборка чатов: закрытые карточки исчезают из Telegram и Max через заданное время (сборка 2026.10.23).

Что убирается: карточка публикации, напоминания-пинги, ответы «✍️ Черновик» / «📈 Динамика» и строки статуса в Max.
Когда: «❌ Ложное» и «⚪ Снято автоматически» — через alerts.cleanup_false_min, «🟢 Решено» — через
alerts.cleanup_resolved_min (по умолчанию 30 и 120 минут; 0 — не убирать). Пауза нужна, чтобы успеть нажать «↩️ Вернуть».
Публикация при этом никуда не девается: она в Радаре (GUI), в отчётах и открывается в чате командой /item N.

Источник правды — текущее состояние публикации, а не расписание: «↩️ Вернуть» или повторный алерт просто выводят
публикацию из-под уборки. Ограничения площадок: Telegram удаляет только сообщения моложе 48 часов (старые карточки
остаются свёрнутыми), Max — по своим правилам; отказ площадки не повторяется бесконечно (до 6 попыток с паузой).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from ..util import ago, iso, jload, now_utc, parse_iso

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger("radar.alerts")

DROPPED_KEY = "card_dropped:"          # kv: когда карточку свернули в «Снято автоматически»
TRY_KEY = "card_cleanup_try:"          # kv: неудачные попытки (сеть, права) — с нарастающей паузой
MAX_TRIES = 6


def delay_min(ctx: "Ctx", state: str) -> int:
    a = ctx.settings.alerts
    return int(a.cleanup_resolved_min if state == "resolved" else a.cleanup_false_min)


def note_line(ctx: "Ctx", item_id: int, state: str) -> str:
    """Строка в свёрнутой карточке: когда она исчезнет и как её найти потом."""
    m = delay_min(ctx, state)
    if m <= 0:
        return ""
    when = f"{m} мин" if m < 120 else f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "")
    return f"🗑 уберу из чата через {when} · потом: /item {item_id} или Радар"


def mark_dropped(ctx: "Ctx", item_id: int) -> None:
    ctx.db.x("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (f"{DROPPED_KEY}{item_id}", f'"{iso()}"'))


def unmark(ctx: "Ctx", item_id: int) -> None:
    ctx.db.x("DELETE FROM kv WHERE key IN (?, ?)", (f"{DROPPED_KEY}{item_id}", f"{TRY_KEY}{item_id}"))


def _has_messages(ctx: "Ctx", item_id: int) -> bool:
    db = ctx.db
    return bool(db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (item_id,)) or
                db.one("SELECT 1 FROM alert_pings WHERE item_id=?", (item_id,)) or
                db.one("SELECT 1 FROM max_msgs WHERE item_id=?", (item_id,)))


def due(ctx: "Ctx", limit: int = 20) -> list[tuple[int, str]]:
    """Публикации, чьи карточки пора убрать: [(id, состояние)]."""
    db, out = ctx.db, []
    instant = ctx.settings.alerts.instant_priorities
    rows = db.q(
        "SELECT i.id, i.workflow, (SELECT MAX(f.ts) FROM feedback f WHERE f.item_id=i.id AND f.verdict=i.workflow) AS at "
        "FROM items i WHERE i.workflow IN ('false_positive','resolved') AND ("
        "EXISTS(SELECT 1 FROM alert_msgs m WHERE m.item_id=i.id) OR EXISTS(SELECT 1 FROM alert_pings p WHERE p.item_id=i.id) "
        "OR EXISTS(SELECT 1 FROM max_msgs x WHERE x.item_id=i.id)) ORDER BY i.id")
    for r in rows:
        m = delay_min(ctx, r["workflow"])
        if m > 0 and (r["at"] or "") <= ago(minutes=m):
            out.append((r["id"], r["workflow"]))
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE ?", (f"{DROPPED_KEY}%",)):
        iid = int(r["key"][len(DROPPED_KEY):])
        it = db.item(iid)
        if not it or it["workflow"] in ("false_positive", "resolved"):   # решение оператора — по своим часам выше
            db.x("DELETE FROM kv WHERE key=?", (r["key"],))
            continue
        if it["workflow"] != "open" or it["priority"] in instant:       # взяли в работу или снова срочно — не трогаем
            db.x("DELETE FROM kv WHERE key=?", (r["key"],))
            continue
        m = delay_min(ctx, "dropped")
        if m > 0 and (jload(r["value"], "") or "") <= ago(minutes=m) and _has_messages(ctx, iid):
            out.append((iid, "dropped"))
    ready = []
    for iid, state in out:
        t = ctx.db.kv_get(f"{TRY_KEY}{iid}") or {}
        if t.get("next", "") > iso():
            continue
        ready.append((iid, state))
    return ready[:limit]


async def sweep(ctx: "Ctx", limit: int = 20) -> int:
    """Раз в минуту: убрать из чатов карточки, которым пора. Возвращает число удалённых сообщений."""
    alerts, db, total = ctx.alerts, ctx.db, 0
    for iid, state in due(ctx, limit):
        async with alerts._card_lock:                 # не пересекаемся с правкой этих же карточек
            n_tg, ok_tg = await alerts.delete_cards(iid)
            n_mx, ok_mx = await alerts.max.delete_cards(iid)
        total += n_tg + n_mx
        # строки статуса для Max, ещё ждущие отправки, после удаления карточки только намусорят
        db.x("UPDATE outbox SET status='dropped', last_error='карточка убрана из чата' WHERE item_id=? AND "
             "status='pending' AND (dedup_key LIKE 'maxstatus:%' OR dedup_key LIKE 'maxaction:%' OR dedup_key LIKE "
             "'ping:%' OR dedup_key LIKE 'maxping:%')", (iid,))
        if ok_tg and ok_mx:
            unmark(ctx, iid)
            if n_tg + n_mx:
                label = {"false_positive": "ложное", "resolved": "решено", "dropped": "снято"}.get(state, state)
                db.event("info", "alerts", f"#{iid} ({label}): карточка убрана из чатов, сообщений: {n_tg + n_mx}")
            continue
        key = f"{TRY_KEY}{iid}"
        t = db.kv_get(key) or {"n": 0}
        t["n"] = int(t.get("n", 0)) + 1
        if t["n"] >= MAX_TRIES:
            # площадка так и не дала удалить (нет прав, сеть): больше не пытаемся, карточка остаётся свёрнутой
            db.x("DELETE FROM alert_msgs WHERE item_id=?", (iid,))
            db.x("DELETE FROM alert_pings WHERE item_id=?", (iid,))
            db.x("DELETE FROM max_msgs WHERE item_id=?", (iid,))
            unmark(ctx, iid)
            db.event("warning", "alerts", f"#{iid}: карточку не удалось убрать из чата за {MAX_TRIES} попыток — "
                                          "проверьте права бота (удаление сообщений); карточка оставлена свёрнутой")
            continue
        t["next"] = iso(now_utc() + timedelta(minutes=min(60, 2 ** t["n"])))
        db.kv_set(key, t)
    return total


def tg_too_old(sent_at: str | None) -> bool:
    """Telegram: сообщение старше 48 ч удалить нельзя — не пытаемся зря."""
    dt = parse_iso(sent_at) if sent_at else None
    return bool(dt and now_utc() - dt > timedelta(hours=47, minutes=50))
