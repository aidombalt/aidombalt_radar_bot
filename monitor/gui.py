"""Веб-интерфейс (NiceGUI): дашборд, упоминания, источники, объекты, наблюдение, мозг, отчёты, настройки, журнал."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from nicegui import app, ui

from .brain.router import local_state
from .entities import import_docx, load_registry
from .pipeline import PLATFORM_TITLES
from .settings import CONFIG_DIR, DATA_DIR
from .util import ago, clip, iso, jload, msk

if TYPE_CHECKING:
    from .context import Ctx
    from .service import Service

PRIO_COLOR = {"P1": "red-7", "P2": "orange-8", "P3": "amber-7", "P4": "grey-5"}
WF = {"open": "новое", "in_work": "в работе", "resolved": "решено", "false_positive": "ложное"}
CU_RATE = {"T4": 1.19, "L4": 1.71, "A100 40GB": 5.40, "A100 80GB": 7.52, "H100": 11.0}
NAV = [("/", "Дашборд", "dashboard"), ("/items", "Упоминания", "forum"), ("/sources", "Источники", "sensors"),
       ("/objects", "Объекты", "apartment"), ("/watch", "Наблюдение", "visibility"), ("/brain", "Мозг", "psychology"),
       ("/reports", "Отчёты", "description"), ("/settings", "Настройки", "settings"), ("/events", "Журнал", "list")]


def setup_gui(ctx: "Ctx", service: "Service") -> None:
    db = ctx.db

    def authed() -> bool:
        return not ctx.settings.gui_password or app.storage.user.get("auth") is True

    @contextmanager
    def frame(title: str):
        ui.colors(primary="#1F3A5F", secondary="#26A69A", accent="#C62828")
        with ui.header().classes("items-center justify-between bg-primary"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("radar", size="md")
                ui.label("Радар · Балтийский Дом").classes("text-lg font-bold")
            with ui.row().classes("gap-0 items-center"):
                for href, name, icon in NAV:
                    ui.button(name, icon=icon, on_click=lambda h=href: ui.navigate.to(h)).props("flat color=white dense no-caps")
                ui.button(icon="power_settings_new", on_click=power_dialog).props("flat round color=white dense") \
                    .tooltip("Выключить или перезапустить Радар")
        with ui.column().classes("w-full max-w-screen-2xl mx-auto p-4 gap-4"):
            ui.label(title).classes("text-2xl font-semibold text-primary")
            yield

    def guard() -> bool:
        if not authed():
            ui.navigate.to("/login")
            return False
        return True

    # ============================================================ питание: выключить / перезапустить (monitor/power.py)
    def power_dialog():
        from . import power

        if not authed():
            return
        try:
            ip = ui.context.client.request.client.host
        except Exception:  # noqa: BLE001
            ip = "?"
        stop_ok, stop_why = power.can_stop()
        restart_ok = power.can_restart()
        with ui.dialog() as dlg, ui.card().classes("w-[480px] max-w-full"):
            ui.label("Питание Радара").classes("text-lg font-bold")
            ui.label("Выключение останавливает сбор, ИИ, ботов и алерты, пока Радар не запустят снова "
                     "(start_radar.bat на ноутбуке). Команде в Telegram и Max уйдёт служебное сообщение.").classes("text-sm")
            if not stop_ok:
                ui.label(stop_why).classes("text-sm text-orange-9")
            if not restart_ok:
                ui.label("Перезапуск доступен, когда Радар запущен через start_radar.bat, systemd или Docker; "
                         "сейчас он запущен вручную — после выключения запустите его снова сами.").classes("text-sm text-grey-7")
            sure = ui.checkbox("Да, выключить: срочные публикации не будут приходить")

            async def go(action: str):
                if action == "stop" and not sure.value:
                    ui.notify("Отметьте галочку подтверждения", type="warning")
                    return
                dlg.close()
                power.request(ctx, action, f"GUI · {ip}")
                with ui.dialog().props("persistent") as bye, ui.card():
                    ui.label("⏹ Радар выключается…" if action == "stop" else "🔄 Радар перезапускается…").classes("text-lg")
                    ui.label("Окно консоли закроется само. Эту вкладку можно закрыть." if action == "stop" else
                             "Через минуту обновите страницу.").classes("text-sm")
                bye.open()
                await power.flush(ctx)
                ui.timer(1.0, app.shutdown, once=True)

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Отмена", on_click=dlg.close).props("flat")
                rb = ui.button("Перезапустить", icon="restart_alt", on_click=lambda: go("restart")).props("outline")
                sb = ui.button("Выключить", icon="power_settings_new", on_click=lambda: go("stop")).props("color=negative")
                if not restart_ok:
                    rb.disable()
                if not stop_ok:
                    sb.disable()
                    sure.disable()
        dlg.open()

    # ============================================================ вход
    @ui.page("/login")
    def login():
        def go():
            if pwd.value == ctx.settings.gui_password:
                app.storage.user["auth"] = True
                ui.navigate.to("/")
            else:
                ui.notify("Неверный пароль", type="negative")

        with ui.card().classes("absolute-center w-80"):
            ui.label("Радар · вход").classes("text-xl font-bold")
            pwd = ui.input("Пароль", password=True, password_toggle_button=True).on("keydown.enter", go).classes("w-full")
            ui.button("Войти", on_click=go).classes("w-full")

    # ============================================================ общие виджеты
    def item_dialog(iid: int):
        it = db.item(iid)
        if not it:
            ui.notify("Не найдено")
            return
        a = jload(it["analysis_json"], {}) or {}
        rk = a.get("_risk", {})
        with ui.dialog() as dlg, ui.card().classes("w-[900px] max-w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.badge(it["priority"] or "в очереди", color=PRIO_COLOR.get(it["priority"], "grey")).classes("text-lg")
                ui.label(f"#{iid} · {PLATFORM_TITLES.get(it['platform'], it['platform'])} · {msk(it['published_at'])} МСК").classes("text-lg")
            ui.label(f"Автор: {it['author_name'] or '—'} ({it['author_followers'] or 0} подп.) · источник: {it['source_key']}")
            if it["parent_text"]:
                ui.label("К посту:").classes("text-caption")
                ui.label(clip(it["parent_text"], 500)).classes("text-caption italic")
            ui.markdown(f"> {clip(it['text'], 2500)}")
            if a:
                for k, label in (("summary", "Суть"), ("why", "Чем опасно"), ("action", "Что делать")):
                    if a.get(k):
                        ui.markdown(f"**{label}:** {a[k]}")
                if a.get("reply_draft"):
                    ui.textarea("Черновик ответа от УК", value=a["reply_draft"]).classes("w-full")
                ui.label(f"Риск {rk.get('score', 0)}/100 · {rk.get('combine', '')} · факторы: {', '.join(rk.get('factors', []))}").classes("text-caption")
                ui.label(f"Анализ: {it['analysis_level']} · {a.get('_meta', {}).get('model', '')}").classes("text-caption")
            with ui.row().classes("gap-1 flex-wrap"):
                for t in jload(it["tags"], []) or []:
                    ui.chip(t).props("dense outline")
            ui.label(f"Матчер: {it['match_hint'] or ''}").classes("text-caption text-grey-7")
            note = ui.input("Заметка оператора", value=it["operator_note"] or "").classes("w-full")

            async def act(wf: str):
                ctx.alerts.ack(iid, "GUI", wf)
                if note.value:
                    db.x("UPDATE items SET operator_note=? WHERE id=?", (note.value, iid))
                await ctx.alerts.refresh_cards(iid)       # карточки в Telegram меняются у всех получателей
                ui.notify(f"Статус: {WF.get(wf, wf)}")
                dlg.close()

            with ui.row():
                ui.button("В работу", on_click=lambda: act("in_work")).props("color=primary")
                ui.button("Решено", on_click=lambda: act("resolved")).props("color=positive")
                ui.button("Ложное", on_click=lambda: act("false_positive")).props("color=negative outline")
                if it["workflow"] in ("false_positive", "resolved"):
                    async def undo():
                        ctx.alerts.reopen(iid, "GUI")
                        await ctx.alerts.refresh_cards(iid)
                        ui.notify("Возвращено в работу; если карточку уже убрали из чатов — она придёт заново")
                        dlg.close()

                    ui.button("↩️ Вернуть", on_click=undo).props("color=primary outline")
                ui.button("Эскалировать", on_click=lambda: (ctx.alerts.raise_alert(iid, it["priority"] or "P1", escalation=9),
                                                             ui.notify("Отправлено руководителям"))).props("color=accent outline")
                if it["url"]:
                    ui.link("Открыть публикацию ↗", it["url"], new_tab=True)
                ui.button("Закрыть", on_click=dlg.close).props("flat")
        dlg.open()

    def items_rows(where: str, args: tuple, limit: int = 300) -> list[dict]:
        # порядок — по дате публикации (колонка «Дата»), а не по времени разбора: пересмотр/повторный разбор старой
        # публикации не должен поднимать её наверх списка
        rows = db.q(f"SELECT * FROM items WHERE {where} ORDER BY COALESCE(published_at, collected_at) DESC LIMIT {limit}", args)
        out = []
        for r in rows:
            a = jload(r["analysis_json"], {}) or {}
            tags = jload(r["tags"], []) or []
            out.append({"id": r["id"], "prio": r["priority"] or "…", "date": msk(r["published_at"]),
                        "ts": r["published_at"] or r["collected_at"] or "",
                        "obj": ", ".join(t.split(":", 1)[1] for t in tags if t.startswith(("ЖК:", "УК:", "сосед:")))[:60],
                        "summary": clip(a.get("summary") or r["text"], 140),
                        "src": PLATFORM_TITLES.get(r["platform"], r["platform"]), "wf": WF.get(r["workflow"], r["workflow"]),
                        "risk": round(r["risk_score"] or 0), "tags": " ".join(tags)})
        return out

    ITEM_COLS = [{"name": "prio", "label": "P", "field": "prio", "sortable": True},
                 {"name": "risk", "label": "Риск", "field": "risk", "sortable": True},
                 {"name": "date", "label": "Дата", "field": "ts", "sortable": True},   # сортировка по ISO-времени
                 {"name": "obj", "label": "Объекты", "field": "obj", "align": "left"},
                 {"name": "summary", "label": "Суть", "field": "summary", "align": "left", "style": "white-space: normal"},
                 {"name": "src", "label": "Площадка", "field": "src"},
                 {"name": "wf", "label": "Статус", "field": "wf"}]

    def items_table(rows: list[dict]):
        t = ui.table(columns=ITEM_COLS, rows=rows, row_key="id",
                     pagination={"rowsPerPage": 20, "sortBy": "date", "descending": True}).classes("w-full")
        t.add_slot("body-cell-date", """<q-td :props="props">{{props.row.date}}</q-td>""")
        t.add_slot("body-cell-prio", """<q-td :props="props"><q-badge :color="{P1:'red-7',P2:'orange-8',P3:'amber-7'}[props.value]||'grey-5'">{{props.value}}</q-badge></q-td>""")
        t.on("rowClick", lambda e: item_dialog(int(e.args[1]["id"])))
        return t

    def live(tbl, fetch, every: float = 15, status=None, enabled=None):
        """Живое обновление таблицы: раз в every секунд перечитать строки и обновить ТОЛЬКО если что-то изменилось
        (новые публикации, приоритет, статус, риск). Сортировка, страница и открытая карточка не сбрасываются.
        О новых P1/P2 — всплывающее уведомление."""
        state = {"sig": tuple((r["id"], r["prio"], r["wf"], r["risk"]) for r in tbl.rows), "ids": {r["id"] for r in tbl.rows}}

        def tick():
            if enabled is not None and not enabled.value:
                return
            rows = fetch()
            sig = tuple((r["id"], r["prio"], r["wf"], r["risk"]) for r in rows)
            if status is not None:
                status.text = f"обновлено {msk(iso(), '%H:%M:%S')}"
            if sig == state["sig"]:
                return
            hot = [r for r in rows if r["id"] not in state["ids"] and r["prio"] in ("P1", "P2")]
            state["sig"], state["ids"] = sig, {r["id"] for r in rows}
            tbl.rows = rows
            tbl.update()
            if hot:
                ui.notify(f"Новые срочные: " + ", ".join(f"{r['prio']} #{r['id']}" for r in hot[:5]), type="warning")

        return ui.timer(every, tick)

    # ============================================================ дашборд
    @ui.page("/")
    def dashboard():
        if not guard():
            return
        with frame("Дашборд"):
            cards = ui.row().classes("w-full gap-4")

            def draw_cards():
                cards.clear()
                st = db.stats()
                last = st["last_run"] or {}
                info = ctx.brain.colab_info or {}
                data = [
                    ("Последний цикл", msk(last.get("finished_at")), f"{last.get('tier', '—')} · новых {last.get('n_new', 0)}", "schedule"),
                    ("P1 / P2 / P3 за 24 ч", f"{st['p1_24h']} / {st['p2_24h']} / {st['p3_24h']}", f"кандидатов {st['cand_24h']} из {st['items_24h']}", "warning"),
                    ("Источники", f"{st['sources'] - st['sources_bad']} / {st['sources']}", f"на паузе {st['sources_open']}", "sensors"),
                    ("Colab", "онлайн" if ctx.brain.colab_online else "офлайн", f"{info.get('model', '')} {info.get('gpu', '')}", "cloud"),
                    ("Локальная модель", "онлайн" if ctx.brain.local_online else ("проверка…" if ctx.brain.local_checked is None
                                                                                    else "офлайн"),
                     (ctx.brain.local_note or ctx.settings.brain.local.model.split("/")[-1])[:60], "memory"),
                    ("Очередь ИИ", str(st["queue"]["queued"]), f"срочных {st['queue']['urgent']}", "queue"),
                    ("Telegram", "OK" if ctx.alerts.tg_ok is not False else "сбой → почта/Colab", "доставка алертов", "send"),
                ]
                with cards:
                    for title, val, sub, icon in data:
                        with ui.card().classes("min-w-[190px]"):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(icon, color="primary")
                                ui.label(title).classes("text-caption")
                            ui.label(val).classes("text-2xl font-bold")
                            ui.label(sub).classes("text-caption text-grey-7")

            draw_cards()
            with ui.row().classes("gap-2"):
                ui.button("Скан: горячие", icon="bolt", on_click=lambda: ui.notify("Запущено" if service.run_now("hot") else "Уже идёт"))
                ui.button("Скан: всё тёплое", icon="travel_explore", on_click=lambda: ui.notify("Запущено" if service.run_now("warm") else "Уже идёт"))

                async def rep():
                    ui.notify("Готовлю отчёт за 24 ч…")
                    from .reports import builder

                    r = await builder.build(ctx, hours=24, kind="manual", send=False, router=service.router)
                    ui.download(r["xlsx"])
                    ui.download(r["docx"])

                ui.button("Отчёт за 24 ч", icon="download", on_click=rep).props("outline")
            ui.label("P1/P2/P3 за 7 дней").classes("text-lg")
            dash_rows = lambda: items_rows("analyzed_at>=? AND priority IN ('P1','P2','P3')", (ago(days=7),))  # noqa: E731
            tbl = items_table(dash_rows())
            live(tbl, dash_rows, 15)
            ui.timer(20, draw_cards)

    # ============================================================ упоминания
    @ui.page("/items")
    def items_page():
        if not guard():
            return
        with frame("Упоминания"):
            with ui.row().classes("items-end gap-3"):
                prio = ui.select(["все", "P1", "P2", "P3", "P4", "в очереди", "кандидаты"], value="все", label="Приоритет").classes("w-40")
                days = ui.number("Дней", value=7, min=1, max=365).classes("w-24")
                q = ui.input("Поиск по тексту/тегам").classes("w-80")
                wf = ui.select(["все"] + list(WF.values()), value="все", label="Статус").classes("w-40")
                auto = ui.switch("Автообновление", value=True)
                upd = ui.label("").classes("text-caption text-grey-7")
            tbl = items_table([])

            def fetch():
                where, args = ["collected_at>=?"], [ago(days=float(days.value or 7))]
                if prio.value in ("P1", "P2", "P3", "P4"):
                    where.append("priority=?")
                    args.append(prio.value)
                elif prio.value == "в очереди":
                    where.append("status IN ('queued','leased')")
                elif prio.value == "кандидаты":
                    where.append("candidate=1")
                else:
                    where.append("(candidate=1)")
                if wf.value != "все":
                    where.append("workflow=?")
                    args.append({v: k for k, v in WF.items()}[wf.value])
                rows = items_rows(" AND ".join(where), tuple(args), 1000)
                if q.value:
                    s = q.value.lower()
                    rows = [r for r in rows if s in (r["summary"] + r["tags"] + r["obj"]).lower()]
                return rows

            def load():
                tbl.rows = fetch()
                tbl.update()
                upd.text = f"обновлено {msk(iso(), '%H:%M:%S')}"

            for w in (prio, days, wf):
                w.on_value_change(load)
            q.on("keydown.enter", load)
            ui.button("Обновить", on_click=load).props("outline")
            load()
            live(tbl, fetch, 15, status=upd, enabled=auto)

    @ui.page("/item/{iid}")
    def item_page(iid: int):
        if not guard():
            return
        with frame(f"Упоминание #{iid}"):
            ui.button("← ко всем", on_click=lambda: ui.navigate.to("/items")).props("flat")
            item_dialog(int(iid))

    # ============================================================ источники
    @ui.page("/sources")
    def sources_page(link: str = ""):
        if not guard():
            return
        with frame("Источники"):
            from .gui_links import sources_page as body
            body(ctx, service, link)

    # ============================================================ объекты
    @ui.page("/objects")
    def objects_page():
        if not guard():
            return
        with frame("Объекты: УК, ЖК, адреса, соседи, кластеры"):
            reg = ctx.reg
            ui.label(f"УК: {len(reg.companies)} · ЖК: {len(reg.zhks)} · адресов: {len(reg.addresses)} · соседей: {len(reg.neighbors)} · кластеров: {len(reg.clusters)}")
            ui.label("Полнота списка наших домов по ЖК").classes("text-lg")
            ui.label("Галочка «список полный» — в ЖК нет наших домов, кроме перечисленных. Тогда публикация про другой "
                     "корпус этого ЖК (например, 13к1 в «Прогрессе») уходит в отчёт без алерта. Без галочки придёт "
                     "P2 «дом не из справочника — проверить», ночью — утром.").classes("text-sm text-gray-600")

            def toggle(key: str, on: bool):
                from .entities import set_zhk_verified

                path = CONFIG_DIR / "entities.yaml"
                (CONFIG_DIR / f"entities.backup.{iso()[:10]}.yaml").write_text(path.read_text("utf-8"), "utf-8")
                if not set_zhk_verified(key, on, f"отмечено в GUI ({iso()[:10]})", iso()[:10], path):
                    return ui.notify("ЖК записан в YAML не одной строкой — поставьте verified вручную ниже", type="warning")
                ctx.reload_registry()
                ui.notify(f"ЖК {ctx.reg.zhk_name(key)}: {'список полный' if on else 'список не проверен'}", type="positive")

            def toggle_shared(key: str, on: bool):
                from .entities import set_zhk_shared

                path = CONFIG_DIR / "entities.yaml"
                (CONFIG_DIR / f"entities.backup.{iso()[:10]}.yaml").write_text(path.read_text("utf-8"), "utf-8")
                if not set_zhk_shared(key, on, f"отмечено в GUI ({iso()[:10]})" if on else "", path):
                    return ui.notify("ЖК записан в YAML не одной строкой — поставьте shared: true вручную ниже", type="warning")
                ctx.reload_registry()
                ui.notify(f"ЖК {ctx.reg.zhk_name(key)}: " + ("есть дома других УК — без нашего адреса в отчёт" if on
                                                          else "все дома наши — название ЖК = наш объект"), type="positive")

            ui.label("Галочка «есть дома других УК» — в ЖК с нами соседствуют другие управляющие компании. Тогда публикация "
                     "с названием ЖК, но без нашего дома и нашей УК уходит в отчёт без алерта (как «ЖК Солнечный город» "
                     "03.11). С нашим адресом, нашей УК или «во всём ЖК» — срочно, как раньше.").classes("text-sm text-gray-600")
            with ui.grid(columns=5).classes("w-full gap-x-6 gap-y-1 items-center text-sm"):
                for h in ("ЖК", "Наших домов", "Список полный", "Есть дома других УК", "Основание"):
                    ui.label(h).classes("font-bold")
                for zk, z in reg.zhks.items():
                    n = sum(1 for a in reg.addresses if a.zhk == zk)
                    ui.label(z.get("name", zk))
                    ui.label(str(n) + (" + без адресов" if any(o.get("zhk") == zk and not o.get("addresses") for c in
                                                             reg.raw.get("companies", []) for o in c.get("objects", [])) else ""))
                    ui.checkbox(value=reg.zhk_complete(zk), on_change=lambda e, k=zk: toggle(k, e.value))
                    ui.checkbox(value=reg.zhk_shared(zk), on_change=lambda e, k=zk: toggle_shared(k, e.value))
                    ui.label(f"{z.get('verified', '')} · {z.get('verified_source', '')}" if z.get("verified") else "—").classes("text-gray-600")
            rows = [{"id": i, "uk": reg.company_name(a.company), "zhk": reg.zhk_name(a.zhk) if a.zhk else "—",
                     "addr": a.raw, "loc": a.locality, "district": a.district or "не указан"} for i, a in enumerate(reg.addresses)]
            ui.table(columns=[{"name": k, "label": l, "field": k, "sortable": True, "align": "left"} for k, l in
                              (("uk", "УК"), ("zhk", "ЖК"), ("addr", "Адрес"), ("loc", "Населённый пункт"), ("district", "Район"))],
                     rows=rows, row_key="id", pagination=15).classes("w-full")
            ui.label("Решения операторов из бота (🏠 Наш дом / 🚫 Не наш)").classes("text-lg")
            ui.label("Файл config/entities.learned.yaml — обновление архива его не затирает. Если дом сменил УК, "
                     "удалите запись.").classes("text-sm text-gray-600")

            @ui.refreshable
            def learned_list():
                from .entities import load_learned, save_learned

                data = load_learned()
                rows = [("foreign_buildings", i, "🚫 не наш", x) for i, x in enumerate(data["foreign_buildings"])] + \
                       [("our_buildings", i, "🏠 наш", x) for i, x in enumerate(data["our_buildings"])]
                if not rows:
                    ui.label("Пока нет записей").classes("text-sm")
                for kind, i, title, x in rows:
                    def drop(kind=kind, i=i):
                        d = load_learned()
                        d[kind].pop(i)
                        save_learned(d)
                        ctx.reload_registry()
                        learned_list.refresh()
                        ui.notify("Запись удалена, справочник применён", type="positive")
                    with ui.row().classes("items-center gap-3"):
                        zk = reg.zhk_name(x.get("zhk", "")) if x.get("zhk") else "—"
                        ui.label(f"{title}: {x.get('address')} · ЖК {zk} · {x.get('by', '')} {str(x.get('at', ''))[:16]} · #{x.get('item', '')}")
                        ui.button("Удалить", on_click=drop).props("flat dense color=negative")

            learned_list()
            ui.label("Справочник (YAML). Сохранение сразу перестраивает матчер.").classes("text-lg")
            path = CONFIG_DIR / "entities.yaml"
            editor = ui.codemirror(path.read_text("utf-8"), language="YAML").classes("w-full h-[520px]")

            def save():
                try:
                    data = yaml.safe_load(editor.value)
                    tmp = CONFIG_DIR / ".entities.check.yaml"
                    tmp.write_text(editor.value, "utf-8")
                    load_registry(tmp)
                    tmp.unlink()
                    assert isinstance(data, dict) and data.get("companies")
                except Exception as ex:
                    return ui.notify(f"Ошибка в YAML: {ex}", type="negative", multi_line=True)
                (CONFIG_DIR / f"entities.backup.{iso()[:10]}.yaml").write_text(path.read_text("utf-8"), "utf-8")
                path.write_text(editor.value, "utf-8")
                ctx.reload_registry()
                service.register_virtual_sources()
                ui.notify("Справочник сохранён и применён", type="positive")

            ui.button("Сохранить и применить", on_click=save)

            async def on_upload(e):
                dst = DATA_DIR / "upload_info.docx"
                await e.file.save(dst)
                draft = import_docx(dst)
                out = CONFIG_DIR / "entities.imported.yaml"
                out.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), "utf-8")
                n = sum(len(c["objects"]) for c in draft["companies"])
                ui.notify(f"Импорт: УК {len(draft['companies'])}, адресов {n}. Черновик: config/entities.imported.yaml — "
                          "сверьте и перенесите изменения в справочник.", multi_line=True, timeout=10000)

            ui.upload(label="Импорт «Информация о ГК.docx» (черновик)", on_upload=on_upload, auto_upload=True).props("accept=.docx")

    # ============================================================ наблюдение
    @ui.page("/watch")
    def watch_page(link: str = ""):
        if not guard():
            return
        with frame("Наблюдение: люди на контроле"):
            from .gui_links import watch_page as body
            body(ctx, service, link)

    # ============================================================ мозг
    @ui.page("/brain")
    def brain_page():
        if not guard():
            return
        with frame("Мозг: Colab + локальная модель"):
            info = ctx.brain.colab_info or {}
            with ui.row().classes("gap-4"):
                with ui.card():
                    ui.label("Colab (большая модель)").classes("font-bold")
                    ui.label(("🟢 онлайн" if ctx.brain.colab_online else "⚪️ офлайн") + f" · последний контакт {msk(ctx.brain.last_colab_seen)}")
                    ui.label(f"Модель: {info.get('model', '—')} · GPU: {info.get('gpu', '—')} · {info.get('engine', '')}")
                    ui.label(f"Сессия: {info.get('uptime_min', '—')} мин · обработано: {info.get('done', '—')}")
                    if ctx.brain.tunnel_url:
                        ui.label("Адрес для Colab → 🔑 RADAR_URL:").classes("text-caption")
                        ui.input(value=ctx.brain.tunnel_url).props("readonly dense outlined").classes("w-96")
                with ui.card():
                    ui.label("Локальная модель (страховка 24/7)").classes("font-bold")
                    ui.label(local_state(ctx) + ("" if ctx.brain.local_online else f" · {ctx.settings.brain.local.model}"))
                    ui.label(f"{ctx.settings.brain.local.base_url}")
                with ui.card():
                    ui.label("Очередь").classes("font-bold")
                    qs = db.queue_stats()
                    ui.label(f"ждут: {qs['queued']} (срочных {qs['urgent']}) · в работе: {qs['leased']}")
                    ui.label(f"старейший в очереди: {msk(qs['oldest'])}")
            ui.label("Калькулятор Colab CU").classes("text-lg")
            with ui.row().classes("items-end gap-3"):
                gpu = ui.select(list(CU_RATE), value="L4", label="GPU").classes("w-40")
                hours = ui.number("Часов в день", value=3, min=0, max=24).classes("w-32")
                res = ui.label()

                def calc():
                    month = CU_RATE[gpu.value] * float(hours.value or 0) * 30
                    res.text = (f"≈ {month:.0f} CU/мес (Colab Pro даёт 100). "
                                + ("✅ укладываетесь" if month <= 100 else f"⚠️ нужно докупить ≈{month - 100:.0f} CU"))

                gpu.on_value_change(calc)
                hours.on_value_change(calc)
                calc()
            ui.markdown(f"""**Как запустить Colab-мозг:** откройте `colab/brain_worker.ipynb` в Colab → в «Секретах» (🔑) добавьте
`RADAR_URL` = `{ctx.settings.public_url or 'https://ваш-сервер'}` и `RADAR_TOKEN` = значение BRIDGE_TOKEN из `.env` →
Среда выполнения → Сменить тип → GPU (L4 или A100) → «Выполнить всё». Ноутбук сам выберет модель под выданную GPU.""")
            ui.label("Проверить текст (ретро-проверка)").classes("text-lg")
            txt = ui.textarea("Вставьте текст поста/комментария", value="Александр Юрьевич, в ЖК Лампо в Мурино УК Возрождение "
                                                                       "совсем не убирает, лифты стоят неделю, помогите!").classes("w-full")
            out = ui.markdown()

            async def run():
                from .tools_replay import replay

                out.content = "⏳ разбор…"
                r = await replay(ctx, service, txt.value)
                a = r["analysis"]
                out.content = (f"**Итог: {r['priority']}** (риск {r['score']:.0f}/100; {r['why']})  \n"
                               f"Кандидат: {r['candidate']} · срочность: {r['urgency']} · {r['reason']}  \n"
                               f"Матчер: {r['hint']}  \n**Суть:** {a.get('summary')}  \n**Что делать:** {a.get('action')}  \n"
                               f"Факторы: {', '.join(r['factors'])}  \nАнализ: {r['level']} {r['error']}")

            ui.button("Проверить", on_click=run)

    # ============================================================ отчёты
    @ui.page("/reports")
    def reports_page():
        if not guard():
            return
        with frame("Отчёты"):
            with ui.row().classes("items-end gap-3"):
                hours = ui.number("За сколько часов", value=24, min=1, max=24 * 31).classes("w-40")
                send = ui.switch("Разослать (почта + Telegram)", value=False)

                async def gen():
                    from .reports import builder

                    ui.notify("Формирую…")
                    r = await builder.build(ctx, hours=float(hours.value), kind="manual", send=send.value, router=service.router)
                    ui.notify(f"Готово: значимых {r['items']}, текст — {r['narrative_by']}")
                    draw()

                ui.button("Сформировать", on_click=gen)
            box = ui.column().classes("w-full")

            def draw():
                box.clear()
                with box:
                    for r in db.q("SELECT * FROM reports ORDER BY id DESC LIMIT 40"):
                        with ui.row().classes("items-center gap-3"):
                            ui.label(f"#{r['id']} {r['kind']} · {msk(r['period_from'])} — {msk(r['period_to'])} · значимых {r['n_items']} · "
                                     f"{'разослан' if r['sent'] else 'не рассылался'}")
                            for p in (r["xlsx_path"], r["docx_path"]):
                                if p and Path(p).exists():
                                    ui.button(Path(p).suffix[1:].upper(), on_click=lambda p=p: ui.download(p)).props("dense outline")

            draw()

    # ============================================================ настройки
    @ui.page("/settings")
    def settings_page():
        if not guard():
            return
        s = ctx.settings
        with frame("Настройки"):
            fields = {}
            with ui.grid(columns=3).classes("gap-4"):
                for path, label, val in [
                    ("schedule.hot_minutes", "Горячий цикл, мин", s.schedule.hot_minutes),
                    ("schedule.warm_minutes", "Тёплый цикл, мин", s.schedule.warm_minutes),
                    ("schedule.cold_minutes", "Холодный цикл, мин", s.schedule.cold_minutes),
                    ("risk.influencer_followers", "Инфлюенсер от, подписчиков", s.risk.influencer_followers),
                    ("risk.p1_score", "Порог P1", s.risk.p1_score), ("risk.p2_score", "Порог P2", s.risk.p2_score),
                    ("alerts.escalate_after_min", "Эскалация P1 через, мин", s.alerts.escalate_after_min),
                    ("alerts.cleanup_false_min", "Убрать из чатов «Ложное», через мин (0 — нет)", s.alerts.cleanup_false_min),
                    ("alerts.cleanup_resolved_min", "Убрать из чатов «Решено», через мин (0 — нет)", s.alerts.cleanup_resolved_min),
                    ("brain.p1_max_wait_sec", "Срочное ждёт Colab, сек", s.brain.p1_max_wait_sec),
                    ("vk.search_daily_budget", "Бюджет поиска ВК/сутки", s.vk.search_daily_budget)]:
                    fields[path] = ui.number(label, value=val)
            reports = ui.input("Время отчётов (МСК, через запятую)", value=", ".join(s.schedule.reports)).classes("w-96")
            emails = ui.input("Почта для отчётов (через запятую)", value=", ".join(s.report_emails)).classes("w-full")
            proxy = ui.input("Прокси для Telegram Bot API (http://… или socks5://…)", value=s.network.telegram_proxy or "").classes("w-full")
            public = ui.input("Публичный адрес Радара (для ссылок)", value=s.public_url).classes("w-full")
            local_url = ui.input("Локальная модель: адрес", value=s.brain.local.base_url).classes("w-96")
            local_model = ui.input("Локальная модель: имя", value=s.brain.local.model).classes("w-96")
            dense = ui.input("Улицы, где почти все дома наши: «улица без номера дома» → P2 (через запятую, как в справочнике; "
                             "пусто — улица без дома всегда P3, в отчёт)", value=", ".join(s.risk.street_dense)).classes("w-full")

            def save():
                for p, w in fields.items():
                    v = w.value
                    ctx.set_override(p, int(v) if float(v).is_integer() else float(v))
                ctx.set_override("schedule.reports", [x.strip() for x in reports.value.split(",") if x.strip()])
                ctx.set_override("report_emails", [x.strip() for x in emails.value.split(",") if "@" in x])
                ctx.set_override("network.telegram_proxy", proxy.value.strip() or None)
                ctx.set_override("public_url", public.value.strip().rstrip("/"))
                ctx.set_override("brain.local.base_url", local_url.value.strip())
                ctx.set_override("brain.local.model", local_model.value.strip())
                ctx.set_override("risk.street_dense", [x.strip() for x in dense.value.split(",") if x.strip()])
                service._build_jobs()
                service.router.reload_local()
                ui.notify("Сохранено и применено (прокси — после перезапуска)", type="positive")

            with ui.row():
                ui.button("Сохранить", on_click=save)

                async def test_tg():
                    ok = 0
                    for chat in ctx.alerts.tg_recipients():
                        try:
                            await ctx.alerts.send_tg({"chat_id": chat, "text": "✅ Тест Радара: Telegram работает"})
                            ok += 1
                        except Exception as ex:
                            hint = " — этот человек ещё не нажал «Старт» у бота" if "chat not found" in str(ex) else ""
                            ui.notify(f"{chat}: {ex}{hint}", type="negative", multi_line=True)
                    ui.notify(f"Отправлено: {ok}")

                async def test_mail():
                    from .alerts.emailer import send_email

                    typed = [x.strip() for x in emails.value.split(",") if "@" in x]
                    if typed != list(ctx.settings.report_emails):   # вписали, но не сохранили — сохраняем за человека
                        ctx.set_override("report_emails", typed)
                    to = typed or ctx.alerts.email_recipients()
                    if not to:
                        ui.notify("Впишите адрес в поле «Почта для отчётов»", type="warning")
                        return
                    try:
                        await send_email(ctx.settings.smtp, to, "Радар мониторинга: проверка доставки",
                                         "<p>Здравствуйте!</p><p>Это проверочное письмо системы мониторинга упоминаний. "
                                         "Если вы его видите, отчёты и срочные уведомления будут приходить на этот адрес.</p>")
                        ui.notify(f"Письмо отправлено: {', '.join(to)}", type="positive")
                    except Exception as ex:
                        hint = (" — Яндекс заподозрил спам: зайдите в этот ящик через браузер, отправьте оттуда одно "
                                "письмо вручную (может попросить подтвердить телефон) и повторите тест") if "SPAM" in str(ex) else ""
                        ui.notify(f"Ошибка почты: {ex}{hint}", type="negative", multi_line=True)

                ui.button("Тест Telegram", on_click=test_tg).props("outline")
                ui.button("Тест почты", on_click=test_mail).props("outline")

            from .alerts.commands import max_status
            max_label = ui.label(max_status(ctx)).classes("text-sm")
            ui.timer(10, lambda: max_label.set_text(max_status(ctx)))
            with ui.row():
                async def test_max():
                    try:
                        await ctx.max_bot.check_health()
                        await ctx.alerts.max.test()
                        ui.notify("Тест поставлен в очередь. Нажмите кнопку в группе Max", type="positive")
                    except Exception as ex:
                        ui.notify(f"Max: {type(ex).__name__}; проверьте токен, группу и HTTPS", type="negative")
                ui.button("Тест Max", on_click=test_max).props("outline")
                ui.switch("Max только при сбое Telegram", value=ctx.settings.max.only_when_tg_down,
                          on_change=lambda e: ctx.set_override("max.only_when_tg_down", bool(e.value)))
                ui.switch("Файлы отчётов в Max", value=ctx.settings.max.report_files,
                          on_change=lambda e: ctx.set_override("max.report_files", bool(e.value)))
                ui.switch("Закреплять невзятый P1", value=ctx.settings.max.pin_p1,
                          on_change=lambda e: ctx.set_override("max.pin_p1", bool(e.value)))
            ui.label("Пользователи (Telegram и Max)").classes("text-lg")
            ucols = [{"name": k, "label": l, "field": k} for k, l in (("tg_id", "Telegram id"), ("max_user_id", "Max id"), ("name", "Имя"), ("role", "Роль"),
                                                                     ("level", "Ур."), ("email", "Почта"), ("active", "Акт."))]
            ut = ui.table(columns=ucols, rows=[dict(r) for r in db.q("SELECT * FROM users")], row_key="tg_id").classes("w-full")
            with ui.row().classes("items-end gap-2"):
                uid = ui.input("Telegram id")
                max_uid = ui.input("Max id (необязательно)")
                uname = ui.input("Имя")
                urole = ui.select(["viewer", "manager", "admin"], value="viewer", label="Роль")
                umail = ui.input("Почта (необязательно)")

                def add_user():
                    mid = max_uid.value.strip()
                    if not uid.value.strip() or (mid and (not mid.isdigit() or not 0 < int(mid) <= 9223372036854775807)):
                        ui.notify("Укажите Telegram ID и корректный числовой Max ID", type="negative")
                        return
                    owner = db.one("SELECT tg_id FROM users WHERE max_user_id=?", (mid,)) if mid else None
                    if owner and owner["tg_id"] != uid.value.strip():
                        ui.notify("Max ID уже привязан к другому пользователю", type="negative")
                        return
                    db.x("INSERT INTO users(tg_id, name, role, email, level, active, added_at) VALUES(?,?,?,?,?,1,?) "
                         "ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name, role=excluded.role, email=excluded.email, "
                         "level=excluded.level, active=1",
                         (uid.value.strip(), uname.value, urole.value, umail.value.strip(), 2 if urole.value != "viewer" else 1, iso()))
                    if mid:
                        db.x("UPDATE users SET max_user_id=? WHERE tg_id=?", (mid, uid.value.strip()))
                    ut.rows = [dict(r) for r in db.q("SELECT * FROM users")]
                    ut.update()

                ui.button("Добавить/обновить", on_click=add_user)
                def unlink_max():
                    if not uid.value.strip():
                        ui.notify("Введите Telegram ID пользователя", type="warning")
                        return
                    db.x("UPDATE users SET max_user_id=NULL WHERE tg_id=?", (uid.value.strip(),))
                    db.x("DELETE FROM account_links WHERE tg_id=?", (uid.value.strip(),))
                    ut.rows = [dict(r) for r in db.q("SELECT * FROM users")]
                    ut.update()
                    ui.notify("Привязка Max снята")
                ui.button("Снять привязку Max", on_click=unlink_max).props("outline")

            from .gui_subs import block as subs_block
            subs_block(ctx)

    # ============================================================ журнал
    @ui.page("/events")
    def events_page():
        if not guard():
            return
        with frame("Журнал событий"):
            rows = [dict(r) | {"ts": msk(r["ts"], "%d.%m %H:%M:%S")} for r in db.q("SELECT * FROM events ORDER BY id DESC LIMIT 300")]
            ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in
                              (("ts", "Время"), ("level", "Уровень"), ("component", "Компонент"), ("message", "Сообщение"))],
                     rows=rows, row_key="id", pagination=30).classes("w-full")
            jobs = [{"name": j.name, "running": "▶" if j.running else "", "ok": msk(j.last_ok, "%d.%m %H:%M:%S"),
                     "err": j.last_error} for j in service.jobs.values()]
            ui.label("Задачи планировщика").classes("text-lg")
            ui.table(columns=[{"name": k, "label": l, "field": k, "align": "left"} for k, l in
                              (("name", "Задача"), ("running", ""), ("ok", "Последний успех"), ("err", "Последняя ошибка"))],
                     rows=jobs, row_key="name").classes("w-full")
