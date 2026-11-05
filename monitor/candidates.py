"""Кандидаты в источники («снежный ком» поиска ВК) — только с доказательствами.

Раньше кандидатом становился любой паблик, где поиск ВК вернул пост по запросу «ЖК Юбилей» — хоть в Выборге.
Теперь паблик предлагается, только если в найденном посте Радар сам узнал наш объект: адрес, УК, или ЖК вместе
с нашим районом. К кандидату прикладываются доказательства — ссылка на пост, цитата, какой объект, — и счётчик.
Старые кандидаты без доказательств при запуске пересматриваются: пустые скрываются (candidate=3), и
возвращаются сами, если позже появится настоящее упоминание.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .util import clip, jdump, jload, msk

if TYPE_CHECKING:
    from .context import Ctx

MIN_MEMBERS = 1000
HIDDEN = 3        # скрыт автоматически (нет доказательств); 2 — отклонён человеком
# Паблики далёких городов: наше там — почти всегда тёзка («ЖК Юбилей» в Выборге). Выборгский район СПб — наш.
FAR_RE = re.compile(r"выборг(?!ск)|кириш|гатчин|тосн|\bлуг[аие]?\b|сосновый бор|кингисепп|тихвин|волхов|приозерск|"
                    r"лодейн|подпорож|бокситогорск|сланц|шлиссельбург|светогорск|каменногорск|новгород|псков|карели|"
                    r"петрозаводск|мурманск|москв|подмосков|калининград|казан|екатеринбург|новосибирск", re.I)


def _label(ctx: "Ctx", h) -> str:
    if h.zhk:
        return f"ЖК {ctx.reg.zhk_name(h.zhk)}"
    if h.company:
        return ctx.reg.company_name(h.company.split(",")[0]).replace("ООО ", "")
    return h.evidence.split(" (")[0]


def evidence(ctx: "Ctx", text: str, url: str, at: str) -> dict | None:
    """Пост доказывает, что паблик про нас: адрес, УК/ГК уверенно — или ЖК плюс наш район."""
    m = ctx.matcher.match(text or "")
    ours = sorted(m.ours, key=lambda h: -h.conf)
    best = next((h for h in ours if h.kind == "address" or (h.kind in ("company", "group") and h.conf >= 0.8)), None)
    if best is None and ours and m.clusters:
        best = ours[0]
    if best is None:
        return None
    return {"url": url, "snip": clip(re.sub(r"\s+", " ", text or ""), 160), "obj": _label(ctx, best),
            "strong": best.kind, "at": at}


def tracked_owner(ctx: "Ctx", oid: int) -> str | None:
    """Паблик уже отслеживается под коротким адресом (kudrovolife = -51766355)?"""
    for r in ctx.db.q("SELECT key, meta_json FROM sources WHERE candidate=0 AND kind='vk_wall' AND meta_json LIKE ?",
                      (f'%"owner_id": {oid}%',)):
        if (jload(r["meta_json"], {}) or {}).get("owner_id") == oid:
            return r["key"]
    return None


def propose(ctx: "Ctx", oid: int, group: dict, ev: dict) -> bool:
    """Найдено упоминание в паблике -oid: завести кандидата или добавить доказательство к существующему."""
    db, key = ctx.db, f"vk_wall:{oid}"
    row = db.one("SELECT candidate, meta_json FROM sources WHERE key=?", (key,))
    if row is None:
        members = group.get("members_count", 0) or 0
        name = group.get("name") or key
        if members < MIN_MEMBERS or tracked_owner(ctx, oid):
            return False
        if FAR_RE.search(name) and ev["strong"] != "address":
            return False
        db.upsert_source({"key": key, "kind": "vk_wall", "handle": str(oid), "title": name, "tier": "warm",
                          "category": "public", "candidate": 1, "members": members, "enabled": 0,
                          "meta": {"owner_id": oid, "screen": group.get("screen_name") or "", "ev": [ev], "n": 1},
                          "note": f"упоминали: {ev['obj']}"})
        return True
    if row["candidate"] not in (1, HIDDEN):
        return False                        # уже источник или отклонён человеком
    meta = jload(row["meta_json"], {}) or {}
    evs = meta.get("ev") or []
    if any(x.get("url") == ev["url"] for x in evs):
        return False
    meta["ev"], meta["n"] = [ev, *evs][:3], int(meta.get("n") or len(evs)) + 1
    objs = sorted({x["obj"] for x in meta["ev"]})
    db.x("UPDATE sources SET candidate=1, meta_json=?, note=? WHERE key=?",
         (jdump(meta), "упоминали: " + ", ".join(objs), key))
    return True


def review(ctx: "Ctx") -> dict:
    """Пересмотр старых кандидатов (до доказательной схемы): дубли и пустые — скрыть, остальным найти доказательства."""
    db, st = ctx.db, {"kept": 0, "hidden": 0, "dup": 0}
    for s in db.q("SELECT key, handle, title, meta_json FROM sources WHERE candidate=1 AND kind='vk_wall'"):
        meta = jload(s["meta_json"], {}) or {}
        if meta.get("ev"):
            continue
        try:
            oid = int(s["handle"])
        except (TypeError, ValueError):
            continue
        if tracked_owner(ctx, oid):
            db.x("UPDATE sources SET candidate=?, note=? WHERE key=?", (HIDDEN, "уже отслеживается под другим адресом", s["key"]))
            st["dup"] += 1
            continue
        evs = []
        for r in db.q("SELECT url, text, published_at FROM items WHERE url LIKE ? ESCAPE '\\' ORDER BY published_at DESC "
                      "LIMIT 200", (f"https://vk.ru/wall{oid}\\_%",)):
            ev = evidence(ctx, r["text"], r["url"], r["published_at"])
            if ev and not (FAR_RE.search(s["title"] or "") and ev["strong"] != "address"):
                evs.append(ev)
        if evs:
            meta.update(ev=evs[:3], n=len(evs), owner_id=oid)
            db.x("UPDATE sources SET meta_json=?, note=? WHERE key=?",
                 (jdump(meta), "упоминали: " + ", ".join(sorted({x["obj"] for x in evs[:3]})), s["key"]))
            st["kept"] += 1
        else:
            db.x("UPDATE sources SET candidate=?, note=? WHERE key=?",
                 (HIDDEN, "скрыт автоматически: в найденных постах наших объектов нет", s["key"]))
            st["hidden"] += 1
    if st["hidden"] or st["dup"]:
        db.event("info", "discovery", f"кандидаты пересмотрены: оставлено {st['kept']}, скрыто без доказательств "
                                      f"{st['hidden']}, дублей {st['dup']}")
    return st


def listing(ctx: "Ctx") -> list[dict]:
    """Кандидаты для показа: сначала где больше упоминаний, потом по аудитории."""
    out = []
    for s in ctx.db.q("SELECT * FROM sources WHERE candidate=1"):
        meta = jload(s["meta_json"], {}) or {}
        oid = meta.get("owner_id")
        screen = meta.get("screen")
        url = (f"https://vk.com/{screen}" if screen else f"https://vk.com/club{-oid}" if oid and oid < 0
               else f"https://vk.com/{s['handle']}" if s["kind"] == "vk_wall" else s["handle"])
        evs = meta.get("ev") or []
        out.append({"key": s["key"], "title": s["title"] or s["handle"], "url": url, "members": s["members"] or 0,
                    "n": int(meta.get("n") or len(evs)), "ev": evs, "objs": sorted({x["obj"] for x in evs}),
                    "last": max((x.get("at") or "" for x in evs), default="")})
    out.sort(key=lambda c: (-c["n"], -c["members"]))
    return out


def members_h(n: int) -> str:
    """8262 → «8,3 тыс.», 94582 → «94,6 тыс.», 120500 → «120 тыс.»."""
    if n < 1000:
        return str(n)
    v = f"{n / 1000:.1f}".replace(".0", "").replace(".", ",") if n < 100_000 else str(n // 1000)
    return f"{v} тыс."


def decide(ctx: "Ctx", keys: list[str], ok: bool, by: str = "") -> int:
    n = 0
    for k in keys:
        n += ctx.db.x("UPDATE sources SET candidate=?, enabled=? WHERE key=? AND candidate IN (1, 3)",
                      (0 if ok else 2, int(ok), k))
    if n:
        ctx.db.event("info", "discovery", f"кандидаты {'добавлены' if ok else 'отклонены'}: {n}{' — ' + by if by else ''}")
    return n


def mentions(n: int) -> str:
    word = "упоминание" if n % 10 == 1 and n % 100 != 11 else (
        "упоминания" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "упоминаний")
    return f"{n} {word}"


def when(at: str) -> str:
    return msk(at, "%d.%m") if at else "—"

