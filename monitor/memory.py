"""Память агента в три слоя.

  БЫСТРАЯ (минуты–часы)  — «обстановка»: открытые инциденты, что горит по этим объектам за 6 ч,
                           неподтверждённые P1/P2. Кладётся в КАЖДЫЙ промпт (≤ ~1,5 тыс. токенов).
  СРЕДНЯЯ (дни–недели)   — инциденты, профили авторов, суточные дайджесты, тренды по ЖК/УК за 30 дней.
                           Подтягивается под конкретную публикацию и через инструменты агента.
  МЕДЛЕННАЯ (месяцы–годы)— архив всех упоминаний (FTS5 по леммам), знания и правила от операторов
                           (только одобренные человеком — защита от «отравления памяти» через посты),
                           разметка «ложное/подтверждено» как few-shot, разборы кейсов (Родимцева).
"""
from __future__ import annotations

import re

from .db import DB
from .entities import Registry
from .matcher import ADDRESSEE_TITLES
from .textnorm import lemma_string
from .util import ago, clip, iso, jload, msk

PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
FLAT_RE = re.compile(r"(кв\.?|квартир[аеуы])\s*№?\s*\d{1,4}", re.I)


def mask_pii(text: str) -> str:
    text = PHONE_RE.sub("[телефон]", text or "")
    text = EMAIL_RE.sub("[email]", text)
    return FLAT_RE.sub(r"\1 [№]", text)


class Memory:
    def __init__(self, db: DB, reg: Registry, influencer_followers: int = 5000, pii: bool = True):
        self.db, self.reg, self.infl, self.pii = db, reg, influencer_followers, pii

    # ================================================================= БЫСТРАЯ
    def brief(self, item: dict) -> str:
        m = jload(item.get("match_json"), {}) or {}
        keys = {h["key"] for h in m.get("hits", [])} | {h.get("zhk") for h in m.get("hits", []) if h.get("zhk")}
        keys |= set(m.get("clusters", []))
        keys.discard("")
        lines = []
        inc = self.db.q("SELECT * FROM incidents WHERE status='open' AND last_seen>=? ORDER BY last_seen DESC LIMIT 30",
                        (ago(hours=72),))
        rel = [r for r in inc if not keys or any(k and k in (r["entity_key"] or "") for k in keys)] or inc[:5]
        for r in rel[:6]:
            lines.append(f"- инцидент #{r['id']} «{r['title']}»: {r['n_items']} упом., макс {r['max_priority']}, "
                         f"последнее {msk(r['last_seen'])}")
        hot = self.db.q(
            "SELECT priority, COUNT(*) n FROM items WHERE analyzed_at>=? AND priority IN ('P1','P2') GROUP BY priority",
            (ago(hours=6),))
        if hot:
            lines.append("- за 6 ч: " + ", ".join(f"{r['priority']}×{r['n']}" for r in hot))
        unacked = self.db.scalar("SELECT COUNT(*) FROM alerts WHERE acked_at IS NULL AND created_at>=?", (ago(hours=24),), 0)
        if unacked:
            lines.append(f"- неподтверждённых алертов за сутки: {unacked}")
        return "\n".join(lines) or "- спокойно: открытых инцидентов по этим объектам нет"

    # ================================================================= СРЕДНЯЯ
    def author_card(self, author_key: str) -> str:
        if not author_key:
            return "автор неизвестен"
        a = self.db.one("SELECT * FROM authors WHERE key=?", (author_key,))
        if not a:
            return "автор ранее не встречался"
        last = self.db.q("SELECT published_at, priority, summary, text FROM items WHERE author_id=? AND id IS NOT NULL "
                         "ORDER BY published_at DESC LIMIT 5", (author_key,))
        s = (f"автор: {a['name'] if self._public(a['followers']) else 'житель'}; подписчиков {a['followers']}; "
             f"упоминаний у нас {a['n_items']}, из них негативных {a['n_negative']}; впервые {msk(a['first_seen'])}"
             + ("; НА КОНТРОЛЕ (watchlist)" if a["watch"] else "") + (f"; заметка: {a['note']}" if a["note"] else ""))
        for r in last:
            s += f"\n  · {msk(r['published_at'])} {r['priority'] or ''} {clip(r['summary'] or r['text'], 140)}"
        return s

    def entity_card(self, name: str) -> str:
        q = (name or "").lower()
        zk = next((k for k, z in self.reg.zhks.items() if q and (q in z["name"].lower() or q in k)), None)
        ck = next((k for k, c in self.reg.companies.items() if q and (q in c["name"].lower() or q == k)), None)
        key = zk or ck
        if not key:
            return f"объект «{name}» не найден в справочнике"
        addrs = [a for a in self.reg.addresses if a.zhk == key or a.company == key]
        rows = self.db.q("SELECT priority, COUNT(*) n, AVG(sentiment) s FROM items WHERE analyzed_at>=? AND relevant=1 "
                         "AND match_json IS NOT NULL AND EXISTS (SELECT 1 FROM json_each(items.match_json, '$.hits') j "
                         "WHERE json_extract(j.value, '$.key')=? OR json_extract(j.value, '$.zhk')=?) GROUP BY priority",
                         (ago(days=30), key, key))
        inc = self.db.q("SELECT title, n_items, max_priority, last_seen FROM incidents WHERE entity_key LIKE ? "
                        "ORDER BY last_seen DESC LIMIT 5", (f"%{key}%",))
        s = f"{'ЖК ' + self.reg.zhk_name(key) if zk else self.reg.company_name(key)}: {len(addrs)} адресов; " + \
            ", ".join(sorted({f'{a.locality}' for a in addrs if a.locality}))
        s += "\n30 дней: " + (", ".join(f"{r['priority']}×{r['n']} (тон {r['s'] or 0:+.1f})" for r in rows) or "тихо")
        for r in inc:
            s += f"\n  · инцидент «{r['title']}» {r['max_priority']} ×{r['n_items']} до {msk(r['last_seen'])}"
        return s

    def thread(self, item: dict) -> str:
        pe = item.get("parent_external_id")
        if not pe:
            return "это не комментарий"
        parent = self.db.one("SELECT text FROM items WHERE external_id=?", (pe,))
        sib = self.db.q("SELECT author_name, text, likes FROM items WHERE parent_external_id=? AND id<>? "
                        "ORDER BY likes DESC LIMIT 6", (pe, item.get("id", 0)))
        s = f"пост: {clip(parent['text'] if parent else item.get('parent_text', ''), 500)}"
        for r in sib:
            s += f"\n  · [{r['likes']}♥] {clip(mask_pii(r['text']), 200)}"
        return s

    # ================================================================= МЕДЛЕННАЯ
    def similar(self, item: dict, limit: int = 4) -> str:
        body = lemma_string(f"{item.get('title') or ''} {item.get('text') or ''}")
        words = [w for w in body.split() if len(w) > 3][:40]
        rows = [r for r in self.db.fts_search(" ".join(words), limit + 3)
                if r["id"] != item.get("id") and r["analyzed_at"]][:limit]
        return "\n".join(f"  · {msk(r['published_at'])} {r['priority']} «{clip(r['summary'] or r['text'], 150)}»"
                         + (f" — оператор: {r['workflow']}" if r["workflow"] in ("false_positive", "resolved") else "")
                         for r in rows)

    def rules(self) -> str:
        rows = self.db.q("SELECT kind, text FROM knowledge WHERE approved=1 ORDER BY id DESC LIMIT 25")
        return "\n".join(f"  · [{r['kind']}] {clip(r['text'], 300)}" for r in rows)

    def fewshot(self) -> str:
        rows = self.db.q(
            "SELECT f.verdict, i.text, i.priority FROM feedback f JOIN items i ON i.id=f.item_id "
            "WHERE f.verdict IN ('false_positive','confirmed') ORDER BY f.id DESC LIMIT 6")
        return "\n".join(f"  · {'ЛОЖНОЕ' if r['verdict'] == 'false_positive' else 'ВЕРНО'} ({r['priority']}): "
                         f"«{clip(mask_pii(r['text']), 160)}»" for r in rows)

    # ================================================================= пакет для ИИ
    def _public(self, followers: int | None) -> bool:
        return (followers or 0) >= self.infl

    def package(self, item: dict, catalog: str, source: dict | None = None) -> dict:
        source = source or {}
        public = self._public(item.get("author_followers")) or source.get("category") in ("media", "official", "governor")
        it = {k: item.get(k) for k in ("id", "platform", "url", "title", "text", "parent_text", "published_at",
                                        "likes", "comments", "reposts", "views", "author_followers", "source_key",
                                        "external_id", "parent_external_id", "author_id")}
        it["author_name"] = item.get("author_name") if (public or not self.pii) else "житель (имя скрыто)"
        if self.pii:
            it["text"] = mask_pii(it.get("text") or "")
            it["parent_text"] = mask_pii(it.get("parent_text") or "")
        it["source_title"] = source.get("title") or item.get("source_key")
        it["source_category"] = source.get("category", "")
        it["source_region"] = source.get("region", "")
        mem = []
        pe = item.get("parent_external_id")
        if pe:                                  # вердикт оператора по исходному посту — главный контекст для комментария
            p = self.db.one("SELECT id, workflow FROM items WHERE external_id=?", (pe,))
            if p and p["workflow"] in ("false_positive", "resolved"):
                mem.append(f"ВЕТКА: исходный пост #{p['id']} оператор отметил «"
                           + ("Ложное» — не про нас; комментарии к нему, скорее всего, тоже не про нас"
                              if p["workflow"] == "false_positive" else "Решено» — история отработана") + ".")
        if item.get("author_id"):
            mem.append(self.author_card(item["author_id"]))
        sim = self.similar(item)
        if sim:
            mem.append("похожие упоминания:\n" + sim)
        r = self.rules()
        if r:
            mem.append("правила/знания (одобрены операторами):\n" + r)
        fs = self.fewshot()
        if fs:
            mem.append("разметка операторов (примеры):\n" + fs)
        media = jload(item.get("media_json"), []) or []
        return {
            "item": it, "catalog": catalog, "brief": self.brief(item), "memory": "\n".join(mem)[:3500],
            "hint": item.get("match_hint") or "", "images": [m for m in media if isinstance(m, str)][:3],
        }

    async def tool(self, name: str, query: str, item: dict) -> str:
        if name == "memory_search":
            rows = self.db.fts_search(lemma_string(query), 6)
            return "\n".join(f"· {msk(r['published_at'])} {r['priority'] or '—'} «{clip(r['summary'] or r['text'], 180)}»"
                             for r in rows) or "ничего не найдено"
        if name == "author_history":
            return self.author_card(item.get("author_id") or query)
        if name == "thread":
            full = self.db.item(item["id"]) if item.get("id") else item
            return self.thread(full or item)
        if name == "entity":
            return self.entity_card(query)
        return "неизвестный инструмент"

    # ================================================================= запись
    def touch_author(self, item: dict, negative: bool = False) -> None:
        if not item.get("author_id"):
            return
        self.db.x(
            "INSERT INTO authors(key, platform, name, followers, n_items, n_negative, first_seen, last_seen) "
            "VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name, "
            "followers=MAX(followers, excluded.followers), n_items=n_items+1, n_negative=n_negative+excluded.n_negative, "
            "last_seen=excluded.last_seen",
            (item["author_id"], item.get("platform"), item.get("author_name"), item.get("author_followers") or 0,
             int(negative), iso(), iso()))

    def feedback(self, item_id: int, verdict: str, user: str, note: str = "") -> None:
        self.db.x("INSERT INTO feedback(item_id,user,verdict,note,ts) VALUES(?,?,?,?,?)", (item_id, user, verdict, note, iso()))
        wf = {"false_positive": "false_positive", "resolved": "resolved", "in_work": "in_work"}.get(verdict)
        if wf:
            self.db.x("UPDATE items SET workflow=?, operator=?, operator_note=? WHERE id=?", (wf, user, note, item_id))

    def add_knowledge(self, kind: str, text: str, source: str, approved: bool = False, key: str = "") -> int:
        return self.db.x("INSERT INTO knowledge(kind,key,text,source,approved,created_at) VALUES(?,?,?,?,?,?)",
                         (kind, key, text, source, int(approved), iso()))

    def addressee_title(self, code: str) -> str:
        return ADDRESSEE_TITLES.get(code, code)
