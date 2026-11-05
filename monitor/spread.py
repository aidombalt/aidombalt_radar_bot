"""«Раскручивается ли публикация»: для статей СМИ счётчиков просмотров нет, зато видно, кто её подхватил —
перепечатки и пересказы (похожий заголовок/текст в других источниках) и публикации по той же теме после неё."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from .textnorm import lemmas
from .util import iso, jload, parse_iso

if TYPE_CHECKING:
    from .context import Ctx

STOP = set("и в во на по о об от до за из к ко с со у не что как это для при а но же ли бы то все его её их был была "
           "были есть сообщать сообщить год который также жк ук дом".split())


def _key(text: str) -> set[str]:
    return {w for w in lemmas(text) if len(w) > 3 and w not in STOP and not w.isdigit()}


def spread(ctx: "Ctx", item: dict, hours: int = 72) -> dict:
    db = ctx.db
    start = item.get("published_at") or iso()
    end = iso((parse_iso(start) or parse_iso(iso())) + timedelta(hours=hours))
    base_title = item.get("title") or ""
    base = _key(f"{base_title} {(item.get('text') or '')[:600]}")
    reprints = []
    if len(base) >= 5:
        seen = set()
        for r in db.fts_search(" ".join(sorted(base)[:12]), limit=60):
            if r["id"] == item["id"] or not r["published_at"] or not (start <= r["published_at"] <= end):
                continue
            if r["source_key"] == item.get("source_key"):
                continue
            other = _key(f"{r['title'] or ''} {(r['text'] or '')[:600]}")
            sim = len(base & other) / max(1, min(len(base), len(other)))
            if sim >= 0.45 and r["source_key"] not in seen:
                seen.add(r["source_key"])
                src = db.one("SELECT title FROM sources WHERE key=?", (r["source_key"],))
                reprints.append({"source": src["title"] if src else r["source_key"], "at": r["published_at"],
                                 "url": r["url"], "views": r["views"] or 0, "sim": round(sim, 2)})
    reprints.sort(key=lambda x: x["at"])
    fu = ctx.alerts.followups(item) or {}
    return {"reprints": reprints[:10], "story_n": fu.get("n", 0), "story_last": fu.get("source", ""),
            "story_at": fu.get("at", ""), "tags": jload(item.get("tags"), []) or []}
