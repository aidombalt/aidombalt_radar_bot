"""Веб-интерфейс: «Источники» и «Наблюдение» по-человечески. Вставил ссылку → увидел, что это → добавил.
Никаких YAML и кодов «hot/warm»: всё выбирается кнопками с понятными подписями."""
from __future__ import annotations

import asyncio
import urllib.parse
from typing import TYPE_CHECKING

import yaml
from nicegui import ui

from . import candidates as cands
from . import linkadd as la
from .settings import CONFIG_DIR
from .util import clip, msk

if TYPE_CHECKING:
    from .context import Ctx
    from .service import Service

ICON = {"vk": "groups", "tg": "send", "web": "newspaper"}
SEARCH_KINDS = ("vk_search", "gnews")
REGIONS = {"SPB": "Петербург", "LO": "Ленобласть", "": "Не важно"}


def source_url(s: dict) -> str:
    h = s.get("handle") or ""
    return {"vk_wall": f"https://vk.com/{h}", "tg_channel": f"https://t.me/s/{h}"}.get(s.get("kind"), h if h.startswith("http") else "")


def _choice(label: str, options: dict, value: str):
    ui.label(label).classes("text-caption text-grey-7 mt-2")
    return ui.toggle(options, value=value if value in options else next(iter(options))).props(
        "no-caps dense unelevated rounded toggle-color=primary color=grey-3 text-color=grey-9")


def _link_input(placeholder: str, on_find):
    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        inp = ui.input(placeholder=placeholder).props("outlined dense clearable").classes("grow")
        btn = ui.button("Найти", icon="search").props("unelevated")
    async def go():
        if not (inp.value or "").strip():
            return
        btn.props("loading")
        try:
            await on_find(inp.value.strip())
        finally:
            btn.props(remove="loading")
    btn.on_click(go)
    inp.on("keydown.enter", go)
    return inp


def _found_header(f: la.Found, editable_title: bool = True):
    with ui.row().classes("items-center gap-3 w-full no-wrap"):
        ui.icon("person" if f.person else ICON.get(f.platform, "link")).classes("text-4xl text-primary")
        with ui.column().classes("gap-0 grow"):
            title = (ui.input(value=f.title, label="Название").props("dense").classes("w-full text-lg")
                     if editable_title else ui.label(f.title).classes("text-lg font-semibold"))
            ui.label(f.summary()).classes("text-caption text-grey-7")
    if f.sample:
        ui.label(f"«{clip(f.sample, 240)}»").classes("italic text-grey-8 text-sm")
    for n in f.notes:
        with ui.row().classes("items-start gap-1 no-wrap"):
            ui.icon("info").classes("text-amber-8")
            ui.label(n).classes("text-sm text-amber-10")
    return title


# ============================================================================================ источники
def sources_page(ctx: "Ctx", service: "Service", link: str = "") -> None:
    db = ctx.db

    # ---------------- добавить по ссылке
    with ui.card().classes("w-full"):
        ui.label("Добавить источник").classes("text-lg font-semibold")
        ui.label("Вставьте ссылку на Telegram-канал, группу или страницу ВКонтакте, либо адрес сайта СМИ — "
                 "Радар сам определит, что это.").classes("text-sm text-grey-7")

        async def find(value: str):
            pv.clear()
            with pv:
                ui.spinner(size="lg")
            f = await la.preview(ctx, service, value)
            pv.clear()
            with pv:
                render_source_preview(f)

        inp = _link_input("t.me/kudrovo_life · @kudrovo_life · vk.com/murino_live · https://fontanka.ru", find)
        pv = ui.column().classes("w-full")

    def render_source_preview(f: la.Found):
        if f.error:
            ui.label(f"⚠️ {f.error}").classes("text-negative")
            return
        with ui.card().props("flat bordered").classes("w-full"):
            title = _found_header(f)
            if f.person:
                ui.label("Это страница человека. Людей лучше ставить на контроль — тогда Радар узнает их "
                         "комментарии в любых пабликах, а не только на их стене.").classes("text-sm")
                ui.button("Поставить на контроль", icon="person_add",
                          on_click=lambda: ui.navigate.to(f"/watch?link={urllib.parse.quote(f.link)}")).props("unelevated")
                return
            if f.exists:
                ui.label("✅ Этот источник уже отслеживается.").classes("text-positive")
                ui.button("Открыть настройки", on_click=lambda: src_dialog(f.exists)).props("outline")
                return
            twin = la.similar_title(ctx, f.title)
            if twin:
                ui.label(f"Похоже, «{twin}» уже есть в списке (возможно, под другим адресом) — проверьте, чтобы не "
                         "дублировать.").classes("text-sm text-amber-10")
            cat = _choice("Что это", la.CATEGORIES, f.category)
            tier = _choice("Как часто проверять", la.TIERS, f.tier)
            reg = _choice("Регион", REGIONS, f.region)
            cm = ui.switch("Читать комментарии под постами", value=f.comments) if f.platform == "vk" else None
            ui.label("Совет: паблики Мурино, Кудрово и страницы власти — «каждые 5 мин»; СМИ — «каждые 15 мин».") \
                .classes("text-caption text-grey-6")

            async def do_add():
                key = la.add_source(ctx, f, title=title.value, category=cat.value, tier=tier.value, region=reg.value,
                                    comments=cm.value if cm else None, by="веб-интерфейс")
                refresh()
                pv.clear()
                inp.value = ""
                with pv:
                    ui.label(f"✅ «{title.value}» добавлен. Первый опрос…").classes("text-positive")
                    status = ui.label("").classes("text-sm text-grey-8")
                await first_poll(key, status)

            ui.button("Добавить в мониторинг", icon="add", on_click=do_add).props("unelevated color=primary").classes("mt-2")

    async def first_poll(key: str, status):
        src = db.one("SELECT * FROM sources WHERE key=?", (key,))
        coll = service.collectors.get(src["kind"]) if src else None
        if not coll:
            status.text = "Опрос начнётся в ближайшем цикле."
            return
        try:
            st = await asyncio.wait_for(coll.run([{**dict(src), "breaker_until": None}]), 120)
            status.text = (f"Первый опрос: получено новых публикаций {st['new']}, из них для ИИ {st['cand']}."
                           if not st["err"] else f"Первый опрос не удался: {la.human_error('; '.join(st['errors']))}. "
                           "Радар повторит в следующем цикле; если так и не заработает — источник станет красным.")
        except asyncio.TimeoutError:
            status.text = "Первый опрос идёт дольше обычного — результат будет на вкладке позже."
        refresh()

    # ---------------- рекомендованный набор
    pack_block(ctx, service, lambda: refresh())

    # ---------------- список
    with ui.row().classes("w-full items-center gap-4"):
        ui.label("Отслеживаемые").classes("text-lg font-semibold")
        search = ui.input(placeholder="поиск по названию").props("dense outlined clearable").classes("w-72")
        only_bad = ui.switch("Только проблемные")
        show_q = ui.switch("Показать поисковые запросы")
    ui.label("🟢 работает · 🟡 были ошибки · 🔴 не отвечает · ⚪ ещё не опрашивался · ⏸ выключен. "
             "Нажмите на строку, чтобы изменить.").classes("text-caption text-grey-7")
    cols = [{"name": "st", "label": "", "field": "st"},
            {"name": "title", "label": "Источник", "field": "title", "align": "left", "sortable": True},
            {"name": "kind", "label": "Где", "field": "kind", "sortable": True},
            {"name": "cat", "label": "Что это", "field": "cat", "sortable": True},
            {"name": "tier", "label": "Проверка", "field": "tier", "sortable": True},
            {"name": "members", "label": "Аудитория", "field": "members", "sortable": True},
            {"name": "n", "label": "Собрано", "field": "n", "sortable": True},
            {"name": "ok", "label": "Последний успех", "field": "ok"},
            {"name": "err", "label": "Проблема", "field": "err", "align": "left"}]

    def rows():
        out = []
        for s in db.q("SELECT * FROM sources WHERE candidate=0 ORDER BY enabled DESC, tier, kind, key"):
            if s["kind"] in SEARCH_KINDS and not show_q.value or s["kind"] == "manual":
                continue
            fails = s["consecutive_failures"] or 0
            bad = s["enabled"] and (fails >= 3 or (fails > 0 and not s["last_ok"]))
            if only_bad.value and not bad:
                continue
            icon = "⏸" if not s["enabled"] else "🔴" if bad else "🟢" if s["last_ok"] else "🟡" if fails else "⚪"
            out.append({"key": s["key"], "st": icon, "title": s["title"] or s["handle"],
                        "kind": la.KINDS.get(s["kind"], s["kind"]), "cat": la.CATEGORIES.get(s["category"], s["category"] or ""),
                        "tier": la.TIERS.get(s["tier"], s["tier"]), "members": s["members"] or "",
                        "n": s["n_items_total"] or 0, "ok": msk(s["last_ok"]) if s["last_ok"] else "—",
                        "err": la.human_error(s["last_error"]) if fails else ""})
        return out

    tbl = ui.table(columns=cols, rows=rows(), row_key="key", pagination=20).classes("w-full").props("flat bordered")
    tbl.bind_filter_from(search, "value")

    def refresh():
        tbl.rows = rows()
        tbl.update()
        draw_cand()

    only_bad.on_value_change(refresh)
    show_q.on_value_change(refresh)

    def src_dialog(key: str):
        s = db.one("SELECT * FROM sources WHERE key=?", (key,))
        if not s:
            return
        s = dict(s)
        with ui.dialog() as dlg, ui.card().classes("w-[680px]"):
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                ui.icon(ICON.get({"vk_wall": "vk", "tg_channel": "tg"}.get(s["kind"], "web"), "link")).classes("text-3xl text-primary")
                title = ui.input(value=s["title"] or s["handle"], label="Название").props("dense").classes("grow")
            url = source_url(s)
            ui.label(f"{la.KINDS.get(s['kind'], s['kind'])} · аудитория {s['members'] or '—'} · собрано {s['n_items_total'] or 0}"
                     ).classes("text-caption text-grey-7")
            if url:
                ui.link("Открыть источник ↗", url, new_tab=True).classes("text-sm")
            if s["last_error"] and (s["consecutive_failures"] or 0):
                ui.label(f"Последняя проблема: {la.human_error(s['last_error'])}").classes("text-sm text-negative")
            en = ui.switch("Отслеживать", value=bool(s["enabled"]))
            cat = _choice("Что это", la.CATEGORIES, s["category"] or "public")
            tier = _choice("Как часто проверять", la.TIERS, s["tier"] or "warm")
            reg = _choice("Регион", REGIONS, s["region"] or "")
            cm = ui.switch("Читать комментарии под постами", value=bool(s["comments"])) if s["kind"] == "vk_wall" else None
            with ui.expansion("Для специалиста").classes("w-full text-sm"):
                ui.label(f"Ключ: {key} · адрес: {s['handle']}").classes("text-caption")
                where = ui.select({"server": "ноутбук/сервер", "colab": "Colab"}, value=s["where_"], label="Кто опрашивает")
                meta = ui.textarea("Служебные параметры (JSON)", value=s["meta_json"] or "{}").classes("w-full")
            out = ui.label("").classes("text-sm")

            def save():
                db.x("UPDATE sources SET title=?, enabled=?, category=?, tier=?, region=?, comments=?, where_=?, meta_json=?, "
                     "breaker_until=NULL, consecutive_failures=0 WHERE key=?",
                     (title.value, int(en.value), cat.value, tier.value, reg.value, int(cm.value) if cm else s["comments"],
                      where.value, meta.value, key))
                ui.notify("Сохранено", type="positive")
                dlg.close()
                refresh()

            async def test():
                coll = service.collectors.get(s["kind"]) or (service.vk_search if s["kind"] == "vk_search" else None)
                if not coll:
                    out.text = "Этот тип проверить отсюда нельзя"
                    return
                out.text = "Проверяю…"
                st = await coll.run([{**dict(db.one("SELECT * FROM sources WHERE key=?", (key,))), "breaker_until": None}])
                out.text = (f"✅ Работает: новых публикаций {st['new']}, для ИИ {st['cand']}" if not st["err"]
                            else f"❌ {la.human_error('; '.join(st['errors']))}")
                refresh()

            def delete():
                with ui.dialog() as conf, ui.card():
                    ui.label(f"Удалить «{title.value}» из мониторинга? Собранные публикации останутся в архиве.")
                    with ui.row():
                        ui.button("Удалить", on_click=lambda: (db.x("DELETE FROM sources WHERE key=?", (key,)), conf.close(),
                                                               dlg.close(), refresh(), ui.notify("Удалено"))).props("color=negative")
                        ui.button("Отмена", on_click=conf.close).props("flat")
                conf.open()

            with ui.row().classes("mt-2"):
                ui.button("Сохранить", on_click=save).props("unelevated")
                ui.button("Проверить сейчас", on_click=test).props("outline")
                ui.button("Удалить", on_click=delete).props("flat color=negative")
        dlg.open()

    tbl.on("rowClick", lambda e: src_dialog(e.args[1]["key"]))

    # ---------------- кандидаты
    ui.label("🆕 Найдены автопоиском — добавить?").classes("text-lg font-semibold mt-4")
    ui.label("Паблики ВК, где поиск нашёл посты про наш адрес, УК или ЖК в нашем районе. Под каждым — доказательство."
             ).classes("text-caption text-grey-7")
    cand_box = ui.column().classes("w-full gap-2")

    def decide(key: str, ok: bool):
        cands.decide(ctx, [key], ok, by="веб-интерфейс")
        refresh()

    def draw_cand():
        cand_box.clear()
        with cand_box:
            rows_c = cands.listing(ctx)[:40]
            if not rows_c:
                ui.label("Новых предложений нет").classes("text-grey-7")
            for c in rows_c:
                with ui.card().props("flat bordered").classes("w-full"):
                    with ui.row().classes("items-start gap-3 w-full no-wrap"):
                        with ui.column().classes("gap-0 grow"):
                            ui.link(c["title"], c["url"], new_tab=True).classes("font-semibold")
                            ui.label(f"ВКонтакте · {cands.members_h(c['members'])} · {cands.mentions(c['n'])} · последнее "
                                     f"{cands.when(c['last'])} · {', '.join(c['objs'])}").classes("text-caption text-grey-7")
                            for ev in c["ev"][:2]:
                                with ui.row().classes("gap-1 items-baseline no-wrap"):
                                    ui.label(f"«{clip(ev['snip'], 150)}»").classes("text-sm italic text-grey-8")
                                    ui.link("пост ↗", ev["url"], new_tab=True).classes("text-sm")
                        ui.button("Добавить", on_click=lambda k=c["key"]: decide(k, True)).props("unelevated dense")
                        ui.button("Не нужно", on_click=lambda k=c["key"]: decide(k, False)).props("flat dense")

    draw_cand()
    if link:
        inp.value = link
        ui.timer(0.1, lambda: find(link), once=True)


PLATFORM_LABEL = {"tg": "Telegram", "vk": "ВКонтакте", "web": "сайт"}
STATUS_ICON = {"added": "✅", "exists": "✅", "dead": "💤", "error": "⚠️", "new": ""}


def pack_block(ctx: "Ctx", service: "Service", on_done) -> None:
    """Рекомендованный набор: галочки → Радар сам открывает каждую ссылку и добавляет только живые."""
    pack = la.load_pack()
    if not pack:
        return
    states = {it["link"]: la.pack_state(ctx, it) for it in pack}
    todo = sum(1 for st, _ in states.values() if st == "new")
    first_time = not ctx.db.kv_get("pack_results", {})
    boxes: dict[str, ui.checkbox] = {}
    stat: dict[str, ui.label] = {}
    with ui.expansion(f"📋 Рекомендованный набор «откуда может рвануть»: {len(pack)} источников, не подключено {todo}",
                      value=first_time).classes("w-full border rounded").props("header-class=text-lg"):
        ui.label("Собран по разбору путей эскалации 2025–2026: канал ЖК → паблик района → Mash/Фонтанка → губернатор "
                 "и «Бастрыкин поручил доложить». ★ — обязательные, отмечены сразу. Перед добавлением Радар открывает "
                 "каждую ссылку: несуществующие, закрытые и молчащие больше 4 месяцев не добавятся."
                 ).classes("text-sm text-grey-8")
        group = None
        for it in pack:
            if it["group"] != group:
                group = it["group"]
                ui.label(group).classes("font-semibold mt-3")
                if it.get("group_note"):
                    ui.label(it["group_note"]).classes("text-caption text-grey-7")
            st, text = states[it["link"]]
            p = la.parse(it["link"])
            where = PLATFORM_LABEL.get(p[0], "") if not isinstance(p, str) else ""
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                done = st in ("added", "exists")
                cb = ui.checkbox(value=(it.get("prio") == 1 and st == "new")).props("dense")
                if done:
                    cb.disable()
                boxes[it["link"]] = cb
                with ui.column().classes("gap-0 grow"):
                    ui.label(f"{'★ ' if it.get('prio') == 1 else ''}{it.get('title') or it['link']} · {where}"
                             ).classes("text-sm" + (" text-grey-6" if done else ""))
                    ui.label(it.get("why", "")).classes("text-caption text-grey-7")
                stat[it["link"]] = ui.label(f"{STATUS_ICON[st]} {text}".strip()).classes(
                    "text-caption text-right w-64 " + ("text-positive" if done else "text-grey-7"))

        budget = ui.label("").classes("text-sm mt-3")
        with ui.row().classes("w-full items-center gap-2"):
            ui.button("Отметить ★", on_click=lambda: [boxes[it["link"]].set_value(True) for it in pack
                                                       if boxes[it["link"]].enabled and it.get("prio") == 1]
                      ).props("flat dense no-caps")
            ui.button("Отметить всё", on_click=lambda: [cb.set_value(True) for cb in boxes.values() if cb.enabled]
                      ).props("flat dense no-caps")
            ui.button("Снять всё", on_click=lambda: [cb.set_value(False) for cb in boxes.values()]).props("flat dense no-caps")
            go = ui.button("Проверить и добавить", icon="playlist_add").props("unelevated color=primary")

    def chosen() -> list[dict]:
        return [it for it in pack if boxes[it["link"]].enabled and boxes[it["link"]].value]

    def update(*_):
        sel = chosen()
        n = la.vk_budget(ctx, [it for it in sel if la.parse(it["link"])[:1] == ("vk",)])
        warn = n > la.VK_DAILY_LIMIT * 0.9
        budget.text = (f"Отмечено {len(sel)}. Нагрузка на ключ ВК: ≈{n} вызовов wall.get в сутки из ~{la.VK_DAILY_LIMIT}"
                       + (" — многовато: снимите часть ВК-источников без ★ или переведите их на «раз в 3 часа»" if warn else ""))
        budget.classes(replace="text-sm mt-3 " + ("text-negative" if warn else "text-grey-8"))
        go.text = f"Проверить и добавить ({len(sel)})"

    for cb in boxes.values():
        cb.on_value_change(update)
    update()

    async def install():
        sel = chosen()
        if not sel:
            return ui.notify("Ничего не отмечено")
        go.props("loading")
        cnt = {"added": 0, "exists": 0, "dead": 0, "error": 0}
        try:
            for it in sel:
                lab = stat[it["link"]]
                lab.text = "⏳ проверяю…"
                try:
                    st, text = await la.install_pack_item(ctx, service, it, by="веб-интерфейс")
                except Exception as e:  # noqa: BLE001
                    st, text = "error", la.human_error(f"{type(e).__name__}: {e}")
                cnt[st] += 1
                lab.text = f"{STATUS_ICON[st]} {text}"
                lab.classes(replace="text-caption text-right w-64 " + (
                    "text-positive" if st in ("added", "exists") else "text-amber-10"))
                if st in ("added", "exists"):
                    boxes[it["link"]].set_value(False)
                    boxes[it["link"]].disable()
                await asyncio.sleep(0.3)      # бережём лимиты ВК и Telegram
        finally:
            go.props(remove="loading")
            update()
            on_done()
        ui.notify(f"Добавлено {cnt['added']}, уже были {cnt['exists']}, пропущено {cnt['dead'] + cnt['error']} "
                  "(причина — рядом с каждой строкой). Первый опрос — в ближайшем цикле.", type="positive", multi_line=True)

    go.on_click(install)


# ============================================================================================ люди
def watch_page(ctx: "Ctx", service: "Service", link: str = "") -> None:
    db = ctx.db
    ui.label("Публикации и комментарии этих людей Радар всегда отдаёт на разбор ИИ, где бы они ни появились. "
             "Их стены и каналы проверяются каждые 5 минут.").classes("text-sm text-grey-8")

    # ---------------- поставить на контроль
    with ui.card().classes("w-full"):
        ui.label("Поставить человека на контроль").classes("text-lg font-semibold")
        ui.label("Вставьте ссылку на страницу ВКонтакте (vk.com/…) или Telegram (@имя, t.me/…).").classes("text-sm text-grey-7")

        async def find(value: str):
            pv.clear()
            with pv:
                ui.spinner(size="lg")
            f = await la.preview(ctx, service, value)
            pv.clear()
            with pv:
                render_person_preview(f)

        inp = _link_input("vk.com/toma_rodimceva · @username · t.me/username", find)
        pv = ui.column().classes("w-full")
        with ui.expansion("Ссылки нет — отслеживать только по имени").classes("w-full text-sm"):
            nm = ui.input("Имя и фамилия").classes("w-96")
            al = ui.input("Другие написания (через запятую)").classes("w-full")
            nt = ui.input("Почему на контроле").classes("w-full")
            ui.button("Поставить на контроль", on_click=lambda: _add([], nm.value, al.value, nt.value)).props("outline")

    def _add(found: list, name: str, aliases: str, note: str):
        name = (name or "").strip()
        if not name:
            return ui.notify("Укажите имя", type="warning")
        la.add_person(ctx, found, name=name, aliases=[a.strip() for a in (aliases or "").split(",") if a.strip()],
                      note=note or "", by="веб-интерфейс")
        ui.notify(f"👤 {name} на контроле", type="positive")
        pv.clear()
        inp.value = ""
        draw_people()

    def render_person_preview(f: la.Found):
        if f.error:
            ui.label(f"⚠️ {f.error}").classes("text-negative")
            return
        with ui.card().props("flat bordered").classes("w-full"):
            _found_header(f, editable_title=False)
            who = la.watched_by_link(ctx, f)
            if who:
                ui.label(f"✅ Уже на контроле: {who}").classes("text-positive")
                return
            if not f.person and f.kind in ("vk_wall", "tg_channel"):
                ui.label("Это сообщество/канал, а не человек. Если это личный канал блогера — можно поставить на "
                         "контроль как человека; если паблик — лучше добавить как источник.").classes("text-sm")
                ui.button("Добавить как источник", icon="add",
                          on_click=lambda: ui.navigate.to(f"/sources?link={urllib.parse.quote(f.link)}")).props("outline")
            known = [p["name"] for p in ctx.watch.people]
            name = ui.input("Как называть", value=f.title if f.person else "").classes("w-96")
            if known:
                ui.label("Если это вторая ссылка уже известного человека — впишите его имя как в списке ниже, ссылка "
                         "добавится к нему.").classes("text-caption text-grey-6")
            aliases = ui.input("Другие написания (через запятую)",
                               value=", ".join(la.default_aliases(f.title)) if f.person else "").classes("w-full")
            note = ui.input("Почему на контроле (для коллег)").classes("w-full")
            ui.button("Поставить на контроль", icon="person_add",
                      on_click=lambda: _add([f], name.value, aliases.value, note.value)).props("unelevated color=primary mt-2")

    # ---------------- список людей
    ui.label("На контроле").classes("text-lg font-semibold mt-2")
    people_box = ui.column().classes("w-full gap-2")

    def last_activity(p: dict) -> str:
        ids = [f"vk:{p['vk_uid']}"] if p.get("vk_uid") else []
        if p.get("tg"):
            ids.append(f"tg:{p['tg']}")
        keys = [f"vk_wall:{p['vk_id']}"] if p.get("vk_id") else []
        if p.get("tg") and p.get("tg_channel") is not False:
            keys.append(f"tg_channel:{p['tg']}")
        if not ids and not keys:
            return ""
        q = ("SELECT published_at, text, url FROM items WHERE author_id IN ({}) OR source_key IN ({}) "
             "ORDER BY published_at DESC LIMIT 1").format(",".join("?" * len(ids)) or "''", ",".join("?" * len(keys)) or "''")
        r = db.one(q, (*ids, *keys))
        return f"Последняя активность {msk(r['published_at'], '%d.%m %H:%M')}: «{clip(r['text'] or '', 120)}»" if r else ""

    def draw_people():
        ctx.reload_registry()
        people_box.clear()
        with people_box:
            if not ctx.watch.people:
                ui.label("Пока никого").classes("text-grey-7")
            for p in ctx.watch.people:
                with ui.card().props("flat bordered").classes("w-full"):
                    with ui.row().classes("items-start gap-3 w-full no-wrap"):
                        ui.icon("person").classes("text-4xl text-primary")
                        with ui.column().classes("gap-1 grow"):
                            ui.label(p["name"]).classes("text-lg font-semibold")
                            with ui.row().classes("gap-2 items-center"):
                                if p.get("vk_id"):
                                    fol = f" · {p['vk_followers']:,} подп.".replace(",", " ") if p.get("vk_followers") else ""
                                    ui.link(f"ВК: {p['vk_id']}{fol}", f"https://vk.com/{p['vk_id']}", new_tab=True).classes("text-sm")
                                if p.get("tg"):
                                    ui.link(f"Telegram: @{p['tg']}", f"https://t.me/{p['tg']}", new_tab=True).classes("text-sm")
                                if not p.get("vk_id") and not p.get("tg"):
                                    ui.label("только по имени").classes("text-sm text-grey-7")
                            if p.get("aliases"):
                                ui.label("Узнаём также как: " + ", ".join(p["aliases"][:6])).classes("text-caption text-grey-7")
                            if p.get("note"):
                                ui.label(p["note"]).classes("text-sm")
                            act = last_activity(p)
                            if act:
                                ui.label(act).classes("text-caption text-grey-8")
                        with ui.column().classes("gap-1 items-end"):
                            ui.button("Изменить", icon="edit", on_click=lambda p=p: edit_person(p)).props("flat dense no-caps")
                            ui.button("Снять с контроля", icon="person_remove", on_click=lambda p=p: confirm_remove(p)) \
                                .props("flat dense no-caps color=negative").classes("whitespace-nowrap")

    def edit_person(p: dict):
        with ui.dialog() as dlg, ui.card().classes("w-[620px]"):
            ui.label(p["name"]).classes("text-lg font-semibold")
            aliases = ui.input("Другие написания (через запятую)", value=", ".join(p.get("aliases", []))).classes("w-full")
            note = ui.textarea("Почему на контроле", value=p.get("note", "")).classes("w-full")
            ui.label("Добавить ещё одну ссылку (например, Telegram к ВК):").classes("text-caption text-grey-7 mt-2")
            extra = ui.input(placeholder="vk.com/… или @имя").props("dense outlined").classes("w-full")
            msg = ui.label("").classes("text-sm")

            async def save():
                found = []
                if (extra.value or "").strip():
                    f = await la.preview(ctx, service, extra.value.strip())
                    if f.error:
                        msg.text = f"⚠️ {f.error}"
                        return
                    found.append(f)
                data = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
                for q in data.get("people", []) or []:
                    if q.get("name") == p["name"]:
                        q["aliases"] = [a.strip() for a in aliases.value.split(",") if a.strip()]
                        q["note"] = note.value
                ctx.save_watchlist(data)
                if found:
                    la.add_person(ctx, found, name=p["name"], by="веб-интерфейс")
                else:
                    ctx.sync_sources_from_yaml()
                dlg.close()
                draw_people()
                ui.notify("Сохранено", type="positive")

            with ui.row():
                ui.button("Сохранить", on_click=save).props("unelevated")
                ui.button("Отмена", on_click=dlg.close).props("flat")
        dlg.open()

    def confirm_remove(p: dict):
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Снять «{p['name']}» с контроля? Собранные публикации останутся в архиве.")
            with ui.row():
                ui.button("Снять", on_click=lambda: (la.remove_person(ctx, p["name"]), dlg.close(), draw_people(),
                                                     ui.notify("Снято с контроля"))).props("color=negative")
                ui.button("Отмена", on_click=dlg.close).props("flat")
        dlg.open()

    draw_people()

    # ---------------- ключевые слова и стоп-листы
    ui.label("Слова и стоп-листы").classes("text-lg font-semibold mt-4")
    data = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
    kw = ui.input_chips("Дополнительные слова для поиска (ловятся в любых падежах)", value=data.get("keywords") or []
                        ).classes("w-full")
    ip = ui.input_chips("Игнорировать публикации с фразами (реклама, розыгрыши)", value=data.get("ignore_phrases") or []
                        ).classes("w-full")
    ia = ui.input_chips("Игнорировать авторов (id: vk:123, tg:канал)", value=[str(x) for x in data.get("ignore_authors") or []]
                        ).classes("w-full")

    def save_lists():
        d = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}
        d["keywords"], d["ignore_phrases"], d["ignore_authors"] = list(kw.value), list(ip.value), list(ia.value)
        ctx.save_watchlist(d)
        service.register_virtual_sources()
        ui.notify("Сохранено и применено", type="positive")

    ui.button("Сохранить слова", on_click=save_lists).props("outline")

    with ui.expansion("Для специалиста: файл watchlist.yaml").classes("w-full text-sm mt-4"):
        path = CONFIG_DIR / "watchlist.yaml"
        ed = ui.codemirror(path.read_text("utf-8"), language="YAML").classes("w-full h-[360px]")

        def save_yaml():
            try:
                d = yaml.safe_load(ed.value) or {}
            except Exception as ex:  # noqa: BLE001
                return ui.notify(f"Ошибка в файле: {ex}", type="negative")
            ctx.save_watchlist(d)
            ctx.sync_sources_from_yaml()
            draw_people()
            ui.notify("Сохранено", type="positive")

        ui.button("Сохранить файл", on_click=save_yaml).props("flat")

    if link:
        inp.value = link
        ui.timer(0.1, lambda: find(link), once=True)
