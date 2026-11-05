"""Word — АНАЛИТИЧЕСКАЯ ЗАПИСКА для чтения (1–2 страницы): состояние, что произошло, инциденты с хронологией,
сюжеты, что сделать, покрытие. Таблицы со всеми упоминаниями — в Excel, здесь их нет."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..util import clip, msk
from . import situation

COLORS = {"P1": RGBColor(0xC6, 0x28, 0x28), "P2": RGBColor(0xEF, 0x6C, 0x00), "P3": RGBColor(0xB2, 0x8C, 0x00)}
LEVEL_FILL = {"red": "FFCDD2", "orange": "FFE0B2", "yellow": "FFF9C4", "green": "C8E6C9"}
GREY = RGBColor(0x60, 0x60, 0x60)


def _shade(par, hex_color: str):
    pPr = par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


def num(n: int) -> str:
    return f"{int(n or 0):,}".replace(",", " ")


def _kv(doc, key: str, value: str):
    p = doc.add_paragraph(style="List Bullet")
    p.add_run(f"{key}: ").bold = True
    p.add_run(value)
    p.paragraph_format.space_after = Pt(0)
    return p


def write(d: dict, path: Path, narrative: str, narrative_by: str) -> Path:
    doc = Document()
    doc.core_properties.author = "Радар · ГК «Балтийский Дом»"
    for sec in doc.sections:
        sec.page_width, sec.page_height = Cm(21), Cm(29.7)
        sec.left_margin = sec.right_margin = Cm(2)
        sec.top_margin = sec.bottom_margin = Cm(1.6)
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    doc.add_heading("Аналитическая записка · упоминания ГК «Балтийский Дом»", level=1)
    sub = doc.add_paragraph()
    r = sub.add_run(f"Период: {msk(d['from'], '%d.%m.%Y %H:%M')} — {msk(d['to'], '%d.%m.%Y %H:%M')} МСК · текст: "
                    + (f"ИИ ({narrative_by})" if narrative_by not in ("", "шаблон") else "по шаблону (модель недоступна)"))
    r.font.size, r.font.color.rgb = Pt(9), GREY

    # ---------------- состояние
    for i, line in enumerate(situation.state_lines(d)):
        p = doc.add_paragraph()
        run = p.add_run(line)
        run.bold = i == 0
        run.font.size = Pt(12 if i == 0 else 11)
        _shade(p, LEVEL_FILL[d["head"]["level"]])
        p.paragraph_format.space_after = Pt(2)

    # ---------------- коротко (модель)
    doc.add_heading("Что произошло", level=2)
    for par in [x.strip() for x in narrative.split("\n") if x.strip()]:
        doc.add_paragraph(par)

    # ---------------- инциденты с хронологией
    if d["matters"]:
        doc.add_heading("Инциденты", level=2)
        for m in d["matters"]:
            p = doc.add_paragraph()
            run = p.add_run(f"{m['priority']} · {', '.join(m['objects']) or m['cluster']} · "
                            f"{situation.TOPIC_TITLES.get(m['topic'], m['topic'])}")
            run.bold, run.font.color.rgb = True, COLORS.get(m["priority"], GREY)
            p.add_run(f" — {m['status']}")
            p.paragraph_format.space_after = Pt(0)
            _kv(doc, "Что", m["summary"])
            _kv(doc, "Когда", f"{msk(m['published_at'])}" + (" (нерабочее время)" if m["off_hours"] else "")
                + f", алерт {msk(m['alerted_at'])}" + (f", взят в работу через {m['reaction']}" if m["reaction"] else
                                                       f", ждёт {m['waiting']}" if m["waiting"] else ""))
            views = (f"; просмотры {num(m['views_first'])} → {num(m['views'])}" if m["views"] > m["views_first"] else
                     f"; просмотров {num(m['views'])}" if m["views"] else "")
            _kv(doc, "Где", m["source"] + (f", подписчиков {num(m['followers'])}" if m["followers"] else "") + views)
            st_ = next((s for s in d["stories"] if s["matter"] == m["id"]), None)
            if st_:
                _kv(doc, "Развитие", f"по теме «{st_['label']}» всего {situation.pubs(st_['n'])}"
                    + (f", после инцидента ещё {st_['after_matter']}" if st_["after_matter"] else "")
                    + (f"; подхватили: {', '.join(st_['sources'][:4])}" if st_["sources"] else ""))
            if m["action"] and m["workflow"] != "resolved":
                _kv(doc, "Что сделать", m["action"])
            if m["reply"] and m["workflow"] != "resolved":
                _kv(doc, "Черновик ответа", clip(m["reply"], 500))
            _kv(doc, "Ссылка", m["url"] or "—")

    # ---------------- сюжеты без инцидента
    rest = [s for s in d["stories"] if not s["matter"]]
    if rest:
        doc.add_heading("Сюжеты в районах", level=2)
        for s in rest[:6]:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(f"{s['label']}: ").bold = True
            p.add_run(f"{situation.pubs(s['n'])}, {msk(s['first'], '%d.%m %H:%M')}–{msk(s['last'], '%d.%m %H:%M')}; "
                      + ("наши объекты: " + ", ".join(s["objects"]) if s["objects"] else "наши объекты не названы")
                      + (f". {s['facts'][0]['text']}" if s["facts"] else ""))

    # ---------------- для контроля
    p3 = [it for it in d["ours"] if it["priority"] == "P3"]
    if p3:
        doc.add_heading("Для контроля (P3)", level=2)
        for it in p3[:8]:
            doc.add_paragraph(f"{msk(it['published_at'])} · {it['_source']}: "
                              f"{it['_a'].get('summary') or clip(it['text'], 200)}", style="List Bullet")

    # ---------------- покрытие
    doc.add_heading("Покрытие", level=2)
    doc.add_paragraph(f"Работают {d['sources_ok']} из {d['sources_enabled']} площадок; собрано {d['collected']} публикаций, "
                      f"для ИИ отобрано {d['candidates']}; разобрано Colab/локально/правилами: "
                      f"{d['levels'].get('colab', 0)}/{d['levels'].get('local', 0)}/{d['levels'].get('rules', 0)}.")
    for n in d["coverage_notes"]:
        doc.add_paragraph(situation.note_icon(n), style="List Bullet")
    foot = doc.add_paragraph()
    r = foot.add_run("Все упоминания, сюжеты, динамика и состояние площадок — в приложенном Excel.")
    r.font.size, r.font.color.rgb = Pt(9), GREY
    doc.save(path)
    return path
