"""Excel — ДАННЫЕ для работы (фильтры, ссылки, цифры). Связный текст — в Word, здесь его нет.

Листы: Сводка (состояние + цифры против прошлого периода) · Инциденты (статус, реакция, охват) · Сюжеты ·
Упоминания (всё значимое, наше сверху) · Лента по часам (видно волну) · Динамика постов · Площадки (только проблемы).
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..pipeline import PLATFORM_TITLES
from ..util import clip, msk
from . import situation

FILL = {"P1": "FFCDD2", "P2": "FFE0B2", "P3": "FFF9C4", "P4": "EEEEEE"}
LEVEL_FILL = {"red": "FFCDD2", "orange": "FFE0B2", "yellow": "FFF9C4", "green": "C8E6C9"}
HEAD = PatternFill("solid", fgColor="1F3A5F")
HFONT = Font(color="FFFFFF", bold=True)
THIN = Border(bottom=Side(style="thin", color="DDDDDD"))
LINK = Font(color="1565C0", underline="single")
WF = {"open": "новое", "in_work": "в работе", "resolved": "решено", "false_positive": "ложное"}


def _header(ws, cols: list[tuple[str, int]], row: int = 1):
    for i, (name, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=name)
        c.fill, c.font = HEAD, HFONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = ws.cell(row=row + 1, column=1)
    ws.row_dimensions[row].height = 30


def _row(ws, r: int, values: list, wrap: set[int] = frozenset(), link_col: int | None = None, url: str | None = None):
    for j, v in enumerate(values, 1):
        c = ws.cell(row=r, column=j, value=v)
        c.alignment = Alignment(wrap_text=j in wrap, vertical="top")
        c.border = THIN
    if link_col and url:
        c = ws.cell(row=r, column=link_col)
        c.hyperlink, c.font = url, LINK


def _delta(now: int, prev: int) -> str:
    if now == prev:
        return "как в прошлый период"
    return f"{'+' if now > prev else '−'}{abs(now - prev)} к прошлому периоду ({prev})"


def write(d: dict, path: Path, narrative: str) -> Path:
    wb = Workbook()
    # ---------------- Сводка: состояние + цифры
    ws = wb.active
    ws.title = "Сводка"
    ws["A1"] = "Радар упоминаний ГК «Балтийский Дом» — данные"
    ws["A1"].font = Font(size=15, bold=True, color="1F3A5F")
    ws["A2"] = f"Период: {msk(d['from'], '%d.%m.%Y %H:%M')} — {msk(d['to'], '%d.%m.%Y %H:%M')} МСК · текст и выводы — в Word"
    r = 4
    for line in situation.state_lines(d):
        c = ws.cell(row=r, column=1, value=line)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        c.fill = PatternFill("solid", fgColor=LEVEL_FILL[d["head"]["level"]])
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        ws.row_dimensions[r].height = 16 * (len(line) // 100 + 1)
        r += 1
    urgent = d["p"].get("P1", 0) + d["p"].get("P2", 0)
    open_n = sum(1 for m in d["matters"] if m["workflow"] in ("open", "in_work"))
    kpi = [("Открытых инцидентов сейчас", open_n, ""),
           ("Срочных (P1+P2) за период", urgent, _delta(urgent, d["prev"]["urgent"])),
           ("Для контроля (P3) про наши объекты", d["p"].get("P3", 0), _delta(d["p"].get("P3", 0), d["prev"]["p3"])),
           ("Сюжетов в районах", len(d["stories"]), ""),
           ("Фон по районам (не про нас)", len(d["background"]), ""),
           ("Вне рабочего времени", len(d["off_hours_items"]), "публикаций из значимых"),
           ("Собрано публикаций / отобрано для ИИ", f"{d['collected']} / {d['candidates']}", ""),
           ("Площадок работает", f"{d['sources_ok']} из {d['sources_enabled']}", ""),
           ("Разобрано: Colab / локальная модель / правила",
            f"{d['levels'].get('colab', 0)} / {d['levels'].get('local', 0)} / {d['levels'].get('rules', 0)}", "")]
    r += 1
    _header(ws, [("Показатель", 44), ("Значение", 16), ("Сравнение", 34), ("", 20)], row=r)
    ws.freeze_panes = None
    for k, v, cmp in kpi:
        r += 1
        _row(ws, r, [k, v, cmp])
        ws.cell(row=r, column=1).font = Font(bold=True)
    if d["objects"]:
        r += 2
        ws.cell(row=r, column=1, value="Наши объекты в упоминаниях").font = Font(bold=True, size=12)
        for obj, n in d["objects"].most_common(15):
            r += 1
            _row(ws, r, [obj, n])
    for n in d["coverage_notes"]:
        r += 2
        c = ws.cell(row=r, column=1, value=situation.note_icon(n))
        c.alignment = Alignment(wrap_text=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        ws.row_dimensions[r].height = 30

    # ---------------- Инциденты
    ws = wb.create_sheet("Инциденты")
    cols = [("Приор.", 7), ("Статус", 30), ("Опубликовано", 13), ("Вне раб. времени", 9), ("Алерт", 12),
            ("Реакция через", 13), ("Объекты", 26), ("Что случилось", 60), ("Источник", 24), ("Просмотры: было → стало", 14),
            ("Что делать", 45), ("Ссылка", 10)]
    _header(ws, cols)
    for i, m in enumerate(d["matters"], 2):
        _row(ws, i, [m["priority"], m["status"], msk(m["published_at"]), "да" if m["off_hours"] else "",
                     msk(m["alerted_at"]), m["reaction"] or (f"ждёт {m['waiting']}" if m["waiting"] else ""),
                     ", ".join(m["objects"]) or m["cluster"], m["summary"], m["source"],
                     (f"{m['views_first']} → {m['views']}" if m["views"] > m["views_first"] else m["views"] or ""),
                     m["action"], "открыть"],
             wrap={2, 7, 8, 11}, link_col=12, url=m["url"])
        ws.cell(row=i, column=1).fill = PatternFill("solid", fgColor=FILL.get(m["priority"], "FFFFFF"))
    if not d["matters"]:
        ws.cell(row=2, column=1, value="Инцидентов (алертов P1/P2) за неделю нет")

    # ---------------- Сюжеты
    ws = wb.create_sheet("Сюжеты")
    cols = [("Сюжет", 34), ("Публикаций", 10), ("Первая", 12), ("Последняя", 12), ("Наши объекты", 26),
            ("Инцидент", 9), ("После инцидента", 10), ("Источники", 40), ("Ключевые факты", 80), ("Ссылки", 12)]
    _header(ws, cols)
    for i, s in enumerate(d["stories"], 2):
        facts = "\n".join(f"• {f['text']} ({f['source']})" for f in s["facts"])
        _row(ws, i, [s["label"], s["n"], msk(s["first"]), msk(s["last"]), ", ".join(s["objects"]) or "не названы",
                     f"#{s['matter']}" if s["matter"] else "", s["after_matter"] or "", ", ".join(s["sources"]), facts,
                     "первая"], wrap={5, 8, 9}, link_col=10, url=s["facts"][0]["url"] if s["facts"] else None)
        ws.row_dimensions[i].height = 15 * max(2, len(s["facts"]) * 2)
    if not d["stories"]:
        ws.cell(row=2, column=1, value="Повторяющихся тем по районам за период нет")

    # ---------------- Упоминания (наше сверху, потом фон)
    ws = wb.create_sheet("Упоминания")
    cols = [("Приор.", 7), ("Про нас", 8), ("Опубликовано", 13), ("Сюжет", 30), ("Объекты", 24), ("Площадка", 12),
            ("Источник", 26), ("Аудитория", 10), ("Кратко", 60), ("Что делать", 40), ("Статус", 10), ("Ссылка", 9),
            ("Текст (начало)", 70)]
    _header(ws, cols)
    for i, it in enumerate(d["items"], 2):
        a = it["_a"]
        _row(ws, i, [it["priority"], "да" if it["_ours"] else "фон", msk(it["published_at"]),
                     it.get("_story", ""),
                     ", ".join(it.get("_objs") or []),
                     PLATFORM_TITLES.get(it["platform"], it["platform"]), it["_source"], it["author_followers"] or "",
                     a.get("summary", ""), a.get("action", "") if it["_ours"] else "", WF.get(it["workflow"], it["workflow"]),
                     "открыть", clip(it["text"], 500)], wrap={4, 5, 9, 10, 13}, link_col=12, url=it.get("url"))
        ws.cell(row=i, column=1).fill = PatternFill("solid", fgColor=FILL.get(it["priority"], "FFFFFF"))
    if d["items"]:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(d['items']) + 1}"
    else:
        ws.cell(row=2, column=1, value="Значимых публикаций за период нет")

    # ---------------- Лента по часам: видно, когда пошла волна
    ws = wb.create_sheet("Лента по часам")
    _header(ws, [("Час (МСК)", 14), ("Про наши объекты", 12), ("Фон по районам", 12)])
    slots = sorted({s for s, _ in d["hourly"]}, key=lambda s: (s[3:5], s[:2], s[6:]))
    for i, s in enumerate(slots, 2):
        _row(ws, i, [s, d["hourly"].get((s, "ours"), 0), d["hourly"].get((s, "bg"), 0)])
    if slots:
        ch = BarChart()
        ch.type, ch.grouping, ch.overlap = "col", "stacked", 100
        ch.title, ch.y_axis.title = "Значимые публикации по часам", "шт."
        ch.add_data(Reference(ws, min_col=2, min_row=1, max_col=3, max_row=len(slots) + 1), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=len(slots) + 1))
        ch.width, ch.height = 24, 10
        ws.add_chart(ch, "E2")

    # ---------------- Динамика постов по инцидентам
    ws = wb.create_sheet("Динамика постов")
    _header(ws, [("Инцидент", 10), ("Замер (МСК)", 14), ("Просмотры", 11), ("Лайки", 8), ("Комментарии", 11),
                 ("Репосты", 9)])
    r = 1
    first_rows = None
    for m in d["matters"]:
        for x in d.get("metrics", {}).get(m["id"], []):
            r += 1
            _row(ws, r, [f"#{m['id']}", msk(x["ts"]), x["views"], x["likes"], x["comments"], x["reposts"]])
            first_rows = first_rows or (r, m["id"])
    if r == 1:
        ws.cell(row=2, column=1, value="Замеров по инцидентам нет")
    elif first_rows:
        n = sum(1 for x in range(2, r + 1) if ws.cell(row=x, column=1).value == f"#{first_rows[1]}")
        if n >= 2:
            ch = LineChart()
            ch.title, ch.y_axis.title = f"Просмотры поста #{first_rows[1]}", "просмотры"
            ch.add_data(Reference(ws, min_col=3, min_row=1, max_row=1 + n), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=2, min_row=2, max_row=1 + n))
            ch.width, ch.height = 22, 9
            ws.add_chart(ch, "H2")

    # ---------------- Не присланы: чужой дом в нашем ЖК, копии отмеченных историй; чужие дома — на пересмотр
    ws = wb.create_sheet("Не присланы")
    _header(ws, [("#", 8), ("Почему", 22), ("Приоритет", 9), ("Опубликовано", 16), ("Текст", 70), ("Основание", 60), ("Ссылка", 12)])
    for i, it in enumerate(d.get("skipped", []), 2):
        _row(ws, i, [it["id"], it["_kind"], it["priority"], msk(it["published_at"]), clip(it.get("text") or "", 300),
                     it["_why"], "открыть"], wrap={5, 6}, link_col=7, url=it.get("url"))
    r0 = len(d.get("skipped", [])) + 3
    ws.cell(row=r0, column=1, value="Дома в наших ЖК, отмеченные как чужие (на пересмотр: если дом стал нашим — удалите "
                                    "запись в GUI → «Объекты»)").font = Font(bold=True)
    for j, f in enumerate(d.get("foreign_review", []), r0 + 1):
        _row(ws, j, ["", "выучено ботом" if f["learned"] else "справочник", "", "", f["address"], f"ЖК {f['zhk']}: {f['note']}", ""],
             wrap={5, 6})

    # ---------------- Площадки: только то, что требует внимания, + итог
    ws = wb.create_sheet("Площадки")
    _header(ws, [("Площадка", 40), ("Тип", 12), ("Последний успех", 16), ("Ошибок подряд", 10), ("Проблема", 60)])
    ws.cell(row=2, column=1, value=f"Работают {d['sources_ok']} из {d['sources_enabled']}; поисковых запросов "
                                   f"(ВК, Google News): {d['searches']}").font = Font(bold=True)
    for i, s in enumerate(d["failing"], 3):
        _row(ws, i, [s["title"] or s["key"], s["kind"], msk(s["last_ok"]), s["consecutive_failures"], s["last_error"] or ""],
             wrap={5})
        ws.cell(row=i, column=1).fill = PatternFill("solid", fgColor="FFCDD2")
    wb.save(path)
    return path
