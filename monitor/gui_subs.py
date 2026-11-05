"""Настройки → «Кому что приходит»: получатели × события, галочки сохраняются сразу."""
from __future__ import annotations

from typing import TYPE_CHECKING

from nicegui import ui

from .alerts import subscriptions as subs

if TYPE_CHECKING:
    from .context import Ctx

CH = {"tg": "Telegram", "mail": "почта", "max": "Max"}


def block(ctx: "Ctx") -> None:
    s = ctx.settings
    ui.label("Кому что приходит").classes("text-lg mt-4")
    ui.label("Галочки сохраняются сразу. Карточки в Telegram (взял в работу, решено) правятся на месте — новых сообщений "
             "не добавляют. Руководству по умолчанию уходят только P1, эскалации, утренний и недельный отчёты."
             ).classes("text-sm text-grey-8")
    with ui.row().classes("items-center gap-6"):
        wh = ui.input("Рабочее время (МСК, пн–пт)", value=s.schedule.work_hours).classes("w-56")
        night = ui.switch("Ночью и в выходные P2 без надзора, СМИ и массовой аварии — утром", value=s.alerts.night_hold)

        def save_hours():
            ctx.set_override("schedule.work_hours", wh.value.strip() or "09:00-18:00")
            ctx.set_override("alerts.night_hold", bool(night.value))
            ui.notify("Сохранено", type="positive")
        ui.button("Сохранить", on_click=save_hours).props("outline dense")

    ui.label("Каналы: ✈️ — Telegram, ✉️ — почта, M — Max. У кого нет почты или Telegram, "
             "там переключателя нет. Почту человеку можно добавить в таблице пользователей выше.").classes("text-caption text-grey-7")
    box = ui.column().classes("w-full gap-0")

    def toggle(rid: str, ev: str, on: bool):
        cur = next(x for x in subs.rows(ctx) if x["id"] == rid)["events"]
        subs.save(ctx, rid, [x for x in cur if x != ev] + ([ev] if on else []))

    def draw():
        box.clear()
        people = subs.people(ctx)
        with box, ui.element("table").classes("w-full text-sm").style("border-collapse:collapse"):
            with ui.element("tr").style("border-bottom:1px solid #ddd"):
                with ui.element("th").classes("text-left p-1"):
                    ui.label("Кто")
                for ev, label in subs.EVENTS.items():
                    with ui.element("th").classes("p-1 px-2 text-center").style("border-left:1px solid #eee"):
                        ui.label(label).tooltip(subs.HINTS[ev])
                with ui.element("th"):
                    ui.label("")
            if not people:
                with ui.element("tr"), ui.element("td").props("colspan=9"):
                    ui.label("Получателей нет: добавьте пользователя бота выше или почту для отчётов").classes("text-grey-7 p-2")
            for p in people:
                with ui.element("tr").style("border-bottom:1px solid #f0f0f0"):
                    with ui.element("td").classes("p-1"):
                        ui.label(p["name"]).classes("font-medium")
                        sub = [p["role"]] + ([f"TG {p['tg']['address']}"] if p["tg"] else []) + \
                              ([p["mail"]["address"]] if p["mail"] and p["mail"]["address"] != p["name"] else [])
                        ui.label(" · ".join(sub)).classes("text-caption text-grey-7")
                        if p["env_admin"]:
                            ui.label("из .env (TG_ADMINS) — добавьте этот id в пользователи выше с именем и почтой, "
                                     "и строки объединятся").classes("text-caption text-amber-9")
                    for ev in subs.EVENTS:
                        with ui.element("td").classes("text-center px-2").style("border-left:1px solid #eee"):
                            with ui.row().classes("gap-3 justify-center no-wrap"):
                                for ch, icon in (("tg", "✈️"), ("mail", "✉️"), ("max", "M")):
                                    r = p[ch]
                                    if r:
                                        ui.checkbox(icon, value=ev in r["events"],
                                                    on_change=lambda e, rid=r["id"], ev=ev: toggle(rid, ev, e.value)
                                                    ).props("dense size=xs").classes("text-xs")
                    with ui.element("td"):
                        if any(r and r["custom"] for r in (p["tg"], p["mail"], p["max"])):
                            ui.button("сброс", on_click=lambda p=p: ([subs.reset(ctx, r["id"]) for r in (p["tg"], p["mail"], p["max"]) if r],
                                                                    draw())).props("flat dense no-caps size=sm")
    draw()
