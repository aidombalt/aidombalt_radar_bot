"""Кому что приходит. Получатели — пользователи бота (Telegram) и адреса почты; события — P1, P2, эскалации,
отчёты (утренний / дневные / недельный) и служебные сообщения. Настраивается галочками в «Настройках».

По умолчанию (пока галочки не трогали):
  • Telegram, админ/менеджер — всё;  • Telegram, наблюдатель — P1, P2, утренний отчёт;
  • почта сотрудника-менеджера — P1, эскалации, все отчёты;
  • почта руководства (только в списке «почта для отчётов») — P1, эскалации, утренний и недельный отчёты.
Служебное (Colab, релей, перерывы в работе) руководству не уходит.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import Ctx

EVENTS = {
    "p1": "🔴 P1",
    "p2": "🟠 P2",
    "esc": "⏰ Эскалации",
    "rep_morning": "📊 Утренний отчёт",
    "rep_day": "📊 Дневные отчёты",
    "rep_weekly": "📅 Недельный",
    "service": "🔧 Служебные",
    "digest": "📥 Найдено поздно",
    "story": "🧩 Склейки",
}
HINTS = {
    "p1": "критичные публикации — сразу, круглосуточно",
    "p2": "срочные; в нерабочее время — только надзор, СМИ и массовые аварии, остальное утром",
    "esc": "P1, которую никто не взял в работу за 15 минут",
    "rep_morning": "первый отчёт дня по расписанию",
    "rep_day": "остальные отчёты по расписанию",
    "rep_weekly": "итоги недели",
    "service": "Colab, релей, Google News, перерывы в работе Радара",
    "digest": "одна сводка найденного с опозданием",
    "story": "утренняя сводка склеек и чужих домов в пробную неделю",
}
ALL = list(EVENTS)


def _defaults(channel: str, role: str, staff_mail: bool) -> list[str]:
    if channel == "tg":
        return ALL if role in ("admin", "manager") else ["p1", "p2", "rep_morning", "digest", "story"]
    if staff_mail:
        return ["p1", "esc", "rep_morning", "rep_day", "rep_weekly"]
    return ["p1", "esc", "rep_morning", "rep_weekly"]


def rows(ctx: "Ctx") -> list[dict]:
    """Все получатели с их подписками (сохранённые галочки или умолчания)."""
    db = ctx.db
    saved = db.kv_get("subscriptions", {}) or {}
    out, seen = [], set()
    for rid, events in list(saved.items()):
        if rid.startswith("tg:") and not db.kv_get(f"subscriptions_v2:{rid}"):
            saved[rid] = list(dict.fromkeys(events + (["digest"] if "p1" in events or "p2" in events else []) +
                                                   (["story"] if "p2" in events else [])))
    users = [dict(r) for r in db.q("SELECT * FROM users WHERE active=1 ORDER BY role, name")]
    admins = [x.strip() for x in os.environ.get("TG_ADMINS", "").split(",") if x.strip()]
    for u in users + [{"tg_id": a, "name": "администратор (.env)", "role": "admin", "email": ""} for a in admins
                      if a not in {u["tg_id"] for u in users}]:
        rid = f"tg:{u['tg_id']}"
        out.append({"id": rid, "channel": "tg", "address": str(u["tg_id"]), "name": u.get("name") or str(u["tg_id"]),
                    "role": u.get("role") or "viewer", "events": saved.get(rid, _defaults("tg", u.get("role") or "", False)),
                    "custom": rid in saved})
        seen.add(rid)
    staff = {(u.get("email") or "").lower(): u for u in users if u.get("email")}
    for mail in sorted({*(m.strip().lower() for m in ctx.settings.report_emails if "@" in m), *staff}):
        rid = f"mail:{mail}"
        if not mail or rid in seen:
            continue
        u = staff.get(mail)
        is_staff = bool(u and u.get("role") in ("admin", "manager"))
        out.append({"id": rid, "channel": "mail", "address": mail, "name": (u or {}).get("name") or mail,
                    "role": (u or {}).get("role") or "руководство", "events": saved.get(rid, _defaults("mail", "", is_staff)),
                    "custom": rid in saved})
        seen.add(rid)
    if ctx.settings.max.enabled and ctx.settings.max_bot_token:
        chat = ctx.settings.max_admin_chat_id
        if chat:
            rid = f"max:{chat}"
            out.append({"id": rid, "channel": "max", "address": chat, "name": "Админ-группа Max", "role": "группа",
                        "events": saved.get(rid, ctx.settings.max.events), "custom": rid in saved})
        if ctx.settings.max.direct_messages:
            for u in users:
                if not u.get("max_user_id"):
                    continue
                address = f"user:{u['max_user_id']}"; rid = f"max:{address}"
                out.append({"id": rid, "channel": "max", "address": address, "name": u["name"] or address,
                            "role": u["role"], "events": saved.get(rid, []), "custom": rid in saved})
    return out


def people(ctx: "Ctx") -> list[dict]:
    """Для настроек: один человек — одна строка, с его каналами (Telegram и/или почта). Почта, указанная у пользователя
    бота, приклеивается к нему; адреса только из «почты для отчётов» — отдельные люди (обычно руководство)."""
    rs = rows(ctx)
    users = {str(r["tg_id"]): dict(r) for r in ctx.db.q("SELECT * FROM users WHERE active=1")}
    mail_owner = {(u.get("email") or "").lower(): tid for tid, u in users.items() if u.get("email")}
    out: dict[str, dict] = {}
    for r in rs:
        if r["channel"] == "tg":
            key = f"u:{r['address']}"
            p = out.setdefault(key, {"name": r["name"], "role": r["role"], "tg": None, "mail": None,
                                     "env_admin": r["address"] not in users})
            p["tg"] = r
        elif r["channel"] == "max":
            owner = next((tid for tid, u in users.items() if f"user:{u.get('max_user_id')}" == r["address"]), None)
            key = f"u:{owner}" if owner else r["id"]
            p = out.setdefault(key, {"name": r["name"], "role": r["role"], "tg": None, "mail": None, "env_admin": False})
            p["max"] = r
        else:
            owner = mail_owner.get(r["address"])
            key = f"u:{owner}" if owner else f"m:{r['address']}"
            p = out.setdefault(key, {"name": r["name"], "role": r["role"], "tg": None, "mail": None, "env_admin": False})
            p["mail"] = r
    for p in out.values():
        p.setdefault("max", None)
    return sorted(out.values(), key=lambda p: (p["tg"] is None, p["name"].lower()))


def recipients(ctx: "Ctx", event: str, channel: str) -> list[str]:
    return [r["address"] for r in rows(ctx) if r["channel"] == channel and event in r["events"]]


def save(ctx: "Ctx", row_id: str, events: list[str]) -> None:
    saved = ctx.db.kv_get("subscriptions", {}) or {}
    saved[row_id] = [e for e in ALL if e in events]
    ctx.db.kv_set("subscriptions", saved)
    ctx.db.kv_set(f"subscriptions_v2:{row_id}", True)


def reset(ctx: "Ctx", row_id: str) -> None:
    saved = ctx.db.kv_get("subscriptions", {}) or {}
    saved.pop(row_id, None)
    ctx.db.kv_set("subscriptions", saved)
