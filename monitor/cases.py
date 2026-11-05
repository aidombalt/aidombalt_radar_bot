"""«Та же история» — наследование вердикта (дизайн v7, §5).

Отношение same_case(N, R) — «N — копия/перепост той же публикации R»: общее место (ЖК), жёсткая связь (ссылка на R,
общая сумма ≥ 10 000, редкое ФИО, почти-копия по триграммам) и тематическая сверка (IDF-пересечение лемм).
Корни не хранятся, а выводятся из текущих данных (§5.2):
  fp     — оператор отметил «Ложное» (в т.ч. «🚫 Не наш»);
  closed — оператор отметил «Решено» (с 2026.10.23): история отработана, её перепосты не будят людей снова;
  auto   — в текущем match_json есть «чужой корпус нашего дома» (ЖК объяснён), уровень ≤ 2, без блокировок;
  white  — явное «это наше»: «✅ Наше», «🏠 Наш дом», «↩️ Вернуть».
Наследник fp/closed/auto — P3 без своей карточки; копия white — своей карточки тоже не получает (строка в карточке
корня), если не появился новый ключ эскалации.

Решения оператора (fp, closed) действуют и на публикации, где назван НАШ адрес (уровень 3), но только на почти
дословные копии (story.verdict_copy_min) или ссылку на корень; «Ложное» — story.verdict_days, «Решено» — только
story.resolved_hours (почти те же слова через неделю — скорее новая авария) и не для копий со словами «опять/снова». Не наследуют:
копия с претензией к УК, которой не было в корне; копия «решённой» истории, дошедшая до власти/надзора/СМИ или с
призывом, которых у корня не было — история поднялась на новый уровень.
"""
from __future__ import annotations

import math
import re
import time
from typing import TYPE_CHECKING

from . import buildings as bl
from .matcher import ADDRESSEES, HOUSING, NEGATIVE
from .textnorm import lemma, normalize, tokens
from .util import ago, jload, parse_iso

if TYPE_CHECKING:
    from .context import Ctx

WHITE = ("ours", "our_building", "reopened")
MONTHS = ("январ", "феврал", "март", "апрел", "мая", "май", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
STOP = set("""быть который этот весь свой такой также только уже ещё еще очень можно нужно сказать говорить один два три
человек год месяц день неделя время раз новость житель жилец дом квартира район город улица проспект область компания
управлять управляющий сообщить сообщать рассказать стать мочь хотеть свой наш ваш они она оно себя течение около более
менее несколько почти сейчас сегодня вчера завтра полгода минута час сутки""".split()) | HOUSING | NEGATIVE
ADDR = {lemma(w) for ws in ADDRESSEES.values() for p in ws for w in p.split() if len(w) > 3}
MONEY_RE = re.compile(r"(\d{1,3}(?:[\s ]?\d{3})*|\d+)(?:[.,](\d+))?\s*(тыс\w*\.?|млн\w*\.?|миллион\w*|млрд\w*|руб\w*\.?|₽)", re.I)
MULT = {"тыс": 1e3, "млн": 1e6, "мил": 1e6, "млр": 1e9}
URL_RE = re.compile(r"https?://([^\s)»\"]+)")
# «опять нет воды», «снова сломали» — повторение проблемы, а не перепост решённой истории
RECUR_RE = re.compile(r"(?<![а-яё])(?:опять|снова|вновь|повторно|ещё раз|еще раз|в очередной раз|который раз)(?![а-яё])", re.I)
NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ][а-яё]{2,}(?:ов|ова|ев|ева|ин|ина|ский|ская|ого|ой|ему|ым)?)\b")


def item_text(it: dict) -> str:
    return normalize(f"{it.get('title') or ''}\n{it.get('text') or ''}".strip())


def money(t: str, minimum: int = 10000) -> set[int]:
    out = set()
    for m in MONEY_RE.finditer(t):
        n = float(m.group(1).replace(" ", "").replace(" ", "") + ("." + m.group(2) if m.group(2) else ""))
        v = n * MULT.get(m.group(3).lower()[:3], 1)
        if v >= minimum:
            out.add(int(v))
    return out


def is_round(v: int) -> bool:
    """Круглая сумма: не больше двух значащих цифр (100 000, 600 000, 1 500 000)."""
    return len(str(int(v)).rstrip("0")) <= 2


def lem_list(t: str) -> list[str]:
    return [lemma(w) for w, _, _ in tokens(t)]


def trigrams(t: str) -> set:
    ls = [x for x in lem_list(normalize(t)) if x not in STOP]
    return {tuple(ls[i:i + 3]) for i in range(len(ls) - 2)}


class Cases:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self._feat: dict[int, tuple[str, dict]] = {}
        self._df: dict[str, int] = {}
        self._df_at = 0.0
        self._n_docs = 0
        self._place = None
        self._roots: tuple[tuple, float, list[dict]] | None = None    # (ключ свежести, время, корни)

    # ---------------------------------------------------------------- признаки
    @property
    def place_lemmas(self) -> set[str]:
        if self._place is None:
            reg, out = self.ctx.reg, set()
            for z in reg.zhks.values():
                for a in z.get("aliases", []):
                    out |= set(lem_list(normalize(a)))
            for c in reg.companies.values():
                for a in c.get("aliases", []):
                    out |= set(lem_list(normalize(a)))
            for c in reg.clusters:
                for k in c.get("keywords", []) + c.get("streets", []):
                    out |= set(lem_list(normalize(k)))
            for a in reg.addresses:
                out |= set(lem_list(normalize(a.street)))
            self._place = out | {"кудровый", "всеволожск", "всеволожский"}
        return self._place

    def invalidate(self) -> None:
        """Совпадения существующих публикаций пересобраны (новый справочник/правила): корни пересчитать."""
        self._roots = None

    def reset(self) -> None:
        """Справочник изменился: названия мест для тем пересобрать."""
        self._place = None
        self._feat.clear()
        self._roots = None

    def names(self, t: str) -> set[str]:
        out = set()
        for m in NAME_RE.finditer(t):
            a, b = lemma(m.group(1).lower()), lemma(m.group(2).lower())
            if a in ADDR or b in ADDR or a in self.place_lemmas or b in self.place_lemmas or a in STOP:
                continue
            out.add(f"{a} {b}")
        return out

    def theme(self, t: str) -> set[str]:
        pl = self.place_lemmas
        return {x for x in lem_list(t) if len(x) >= 5 and not x[0].isdigit() and x not in STOP and x not in pl
                and x not in ADDR and not x.startswith(MONTHS)}

    def features(self, it: dict) -> dict:
        key = it.get("content_hash") or str(hash(item_text(it)))
        hit = self._feat.get(it.get("id") or 0)
        if hit and hit[0] == key:
            return hit[1]
        t = item_text(it)
        f = {"money": money(t, self.ctx.settings.story.money_min), "names": self.names(t), "tri": trigrams(t),
             "theme": self.theme(t), "text": t}
        if it.get("id"):
            if len(self._feat) > 5000:
                self._feat.clear()
            self._feat[it["id"]] = (key, f)
        return f

    def _refresh(self) -> None:
        if time.time() - self._df_at > 6 * 3600:
            self._df, self._df_at = {}, time.time()
            self._n_docs = self.ctx.db.scalar("SELECT COUNT(*) FROM items_fts f JOIN items i ON i.id=f.rowid "
                                              "WHERE i.collected_at>=?", (ago(days=30),), 0) or 0

    def df(self, term: str) -> int:
        """Документная частота леммы за 30 дней — из существующего FTS-индекса лемм; N — та же совокупность (кеш 6 ч)."""
        self._refresh()
        if term not in self._df:
            try:
                self._df[term] = self.ctx.db.scalar(
                    "SELECT COUNT(*) FROM items_fts f JOIN items i ON i.id=f.rowid WHERE items_fts MATCH ? "
                    "AND i.collected_at>=?", (f'"{term}"', ago(days=30)), 0) or 0
            except Exception:  # noqa: BLE001 — экзотический символ в запросе FTS: считаем термин редким
                self._df[term] = 0
        return self._df[term]

    @property
    def n_docs(self) -> int:
        self._refresh()
        return self._n_docs

    # ---------------------------------------------------------------- отношение
    def same_case(self, n: dict, r: dict) -> tuple[bool, float, list[str]]:
        cfg = self.ctx.settings.story
        fn, fr = self.features(n), self.features(r)
        why = []
        um = URL_RE.search(r.get("url") or "")
        if um:
            core = um.group(1).split("?")[0].rstrip("/")
            if "/" in core and core.split("/", 1)[1] and core in fn["text"]:
                why.append("ссылка на корень")
        mon = fn["money"] & fr["money"]
        round_only = bool(mon) and all(is_round(v) for v in mon)
        if mon:
            why.append("сумма " + ", ".join(f"{v:,}".replace(",", " ") for v in sorted(mon)))
        n_docs = self.n_docs
        nm = {x for x in fn["names"] & fr["names"] if n_docs < 300 or self.df(x.split()[1]) <= 1}
        if nm:
            why.append("имя " + ", ".join(sorted(nm)))
        a, b = fn["tri"], fr["tri"]
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        cont = len(small & big) / max(1, len(small))
        if cont >= cfg.copy_min:
            why.append(f"копия {cont:.2f}")
        ta, tb = fn["theme"], fr["theme"]
        nd = max(n_docs, 1)
        idf = (lambda f: math.log((nd + 1) / (1 + self.df(f))) + 1) if n_docs >= 300 else (lambda f: 1.0)
        th = sum(idf(f) for f in ta & tb) / max(1e-9, min(sum(idf(f) for f in ta), sum(idf(f) for f in tb)))
        need = cfg.round_theme_min if (round_only and len(why) == 1) else cfg.theme_min
        return bool(why) and th >= need, round(th, 2), why

    # ---------------------------------------------------------------- место, уровень, корни
    @staticmethod
    def place(match: dict) -> set[str]:
        out = set()
        for h in (match.get("hits") or []) + (match.get("explained_hits") or []):
            if h.get("kind") == "zhk" and h.get("conf", 0) >= 0.3:
                out.add(h.get("zhk") or h.get("key"))
            elif h.get("kind") == "address" and h.get("zhk"):
                out.add(h["zhk"])
        out |= {f["zhk"] for f in match.get("foreign") or [] if f.get("zhk")}
        out.discard("")
        out.discard(None)
        return out

    @staticmethod
    def level(match: dict) -> int:
        if "raw_level" in match:
            return int(match["raw_level"])
        hits = match.get("hits") or []
        if any(h["kind"] in ("company", "group", "address") and h["conf"] >= 0.45 for h in hits):
            return 3
        return 2 if any(h["kind"] == "zhk" for h in hits) else 1

    def auto_root(self, match: dict) -> bool:
        """Публикация про чужой корпус нашего дома (§5.2): есть сильная связь, нет нашего/частичного, нет блокировок."""
        return bool([f for f in match.get("foreign") or [] if f.get("zhk")]) and self.level(match) <= 2 \
            and not match.get("partial") and not match.get("blocked")

    def root_cap(self, match: dict) -> tuple[str, bool]:
        """Потолок auto-корня: известный чужой адрес или сверенный ЖК → P3, иначе P2 «проверить» (hold)."""
        cfg = self.ctx.settings.risk
        fs = [f for f in match.get("foreign") or [] if f.get("zhk")]
        if any(f.get("known") or self.ctx.reg.zhk_complete(f["zhk"]) for f in fs):
            return cfg.foreign_cap, False
        return cfg.registry_check_cap, True

    def white(self, item_id: int) -> bool:
        r = self.ctx.db.one("SELECT verdict FROM feedback WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,))
        return bool(r and r["verdict"] in WHITE)

    def operator_decided(self, it: dict) -> bool:
        if (it.get("workflow") or "open") != "open":
            return True
        return bool(it.get("id")) and self.white(it["id"])

    OPERATOR = ("fp", "closed")              # корни-решения оператора: живут verdict_days, действуют и на наш адрес

    def days_for(self, kind: str) -> float:
        cfg = self.ctx.settings.story
        if kind == "fp":
            return max(cfg.root_days, cfg.verdict_days)
        if kind == "closed":
            return cfg.resolved_hours / 24
        return cfg.root_days

    def roots(self, exclude_id: int = 0) -> list[dict]:
        """Корни историй. Сверщик спрашивает их для каждой открытой P1/P2 раз в минуту — без кэша это полный проход по
        items на каждый вызов. Кэш живёт ≤15 с и сбрасывается новой публикацией или новым решением оператора."""
        db = self.ctx.db
        key = (db.scalar("SELECT MAX(id) FROM items", (), 0), db.scalar("SELECT MAX(id) FROM feedback", (), 0),
               db.scalar("SELECT COUNT(*) FROM feedback", (), 0))
        if self._roots and self._roots[0] == key and time.monotonic() - self._roots[1] < 15:
            return [r for r in self._roots[2] if r["item"]["id"] != exclude_id]
        out = self._load_roots()
        self._roots = (key, time.monotonic(), out)
        return [r for r in out if r["item"]["id"] != exclude_id]

    def _load_roots(self) -> list[dict]:
        db, cfg = self.ctx.db, self.ctx.settings.story
        rows = db.q("SELECT * FROM items WHERE collected_at>=? AND (workflow IN ('false_positive','resolved') "
                    "OR match_json LIKE '%\"foreign\": [{%' OR id IN (SELECT item_id FROM feedback WHERE verdict IN "
                    "('ours','our_building','reopened'))) ORDER BY id DESC LIMIT 400",
                    (ago(days=max(cfg.root_days, cfg.verdict_days)),))
        short = ago(days=cfg.root_days)
        out = []
        for r in rows:
            r = dict(r)
            m = jload(r.get("match_json"), {}) or {}
            if self.white(r["id"]):
                kind = "white"
            elif r["workflow"] == "false_positive":
                kind = "fp"
            elif r["workflow"] == "resolved":
                kind = "closed"
            elif self.auto_root(m):
                kind = "auto"
            else:
                continue
            if kind not in self.OPERATOR and (r.get("collected_at") or "") < short:
                continue
            if kind == "closed" and (r.get("collected_at") or "") < ago(hours=cfg.resolved_hours * 2):
                continue
            out.append({"item": r, "match": m, "kind": kind, "place": self.place(m)})
        return out

    def copy_ratio(self, n: dict, r: dict) -> float:
        """Доля триграмм меньшего текста, входящих в больший: 1.0 — дословная копия (перепост, пересказ слово в слово)."""
        a, b = self.features(n)["tri"], self.features(r)["tri"]
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        return len(small & big) / max(1, len(small))

    @staticmethod
    def new_keys(match: dict, root: dict) -> list[str]:
        """Что есть у копии и не было у корня: адресат-власть/надзор/СМИ, призыв. Для «Решено» — повод прийти снова."""
        from .pipeline import ESCALATION_ADDR
        from .matcher import ADDRESSEE_TITLES

        rm = jload(root.get("match_json"), {}) or {}
        new = sorted((set(match.get("addressees") or []) & ESCALATION_ADDR) - set(rm.get("addressees") or []))
        out = [ADDRESSEE_TITLES.get(x, x) for x in new]
        if match.get("cta") and not rm.get("cta"):
            out.append("призыв")
        return out

    # ---------------------------------------------------------------- наследование
    def thread_root(self, it: dict, match: dict) -> dict | None:
        """Комментарий под публикацией, которую оператор отметил «Ложное»/«Решено» (или под её копией), наследует
        вердикт: 03.11 пост «вода пахнет в ЖК Солнечный город» отметили «Ложное», а комментарий под ним «Водоканал, ждём
        с проверкой в ЖК Солнечный город» пришёл новым P2. Не наследует, если комментарий называет наш дом или УК, которых
        нет в посте; для «Решено» — ещё и при «опять/снова», новой претензии к УК, власти/СМИ/призыве."""
        pe = it.get("parent_external_id")
        if not pe:
            return None
        row = self.ctx.db.one("SELECT * FROM items WHERE external_id=? AND id<>?", (pe, it.get("id") or 0))
        if not row:
            return None
        parent = dict(row)
        kind, root = {"false_positive": "fp", "resolved": "closed"}.get(parent.get("workflow") or ""), parent["id"]
        if not kind:
            r = ((jload(parent.get("analysis_json"), {}) or {}).get("_risk") or {})
            if r.get("copy_kind") in self.OPERATOR and r.get("copy_of"):
                kind, root = r["copy_kind"], int(r["copy_of"])
        if not kind or self.white(parent["id"]):
            return None
        t0 = parse_iso(it.get("published_at") or it.get("collected_at") or "")
        tp = parse_iso(parent.get("published_at") or parent.get("collected_at") or "")
        if t0 and tp and abs((t0 - tp).total_seconds()) > self.days_for(kind) * 86400:
            return None
        strong = lambda m: {h["key"] for h in (m.get("hits") or [])                      # noqa: E731
                            if h["kind"] in ("company", "group", "address") and h["conf"] >= 0.45}
        if strong(match) - strong(jload(parent.get("match_json"), {}) or {}):
            return None                                   # комментатор назвал наш дом/УК — это уже его история
        if kind == "closed":
            from .subject import uk_claim

            body, pbody = item_text(it), item_text(parent)
            if (uk_claim(body) and not uk_claim(pbody)) or self.new_keys(match, parent) or \
                    (RECUR_RE.search(body) and not RECUR_RE.search(pbody)):
                return None
        verdict = {"fp": "«Ложное»", "closed": "«Решено»"}[kind]
        return {"kind": kind, "root": root, "cap": "P3", "hold": False,
                "why": f"комментарий к публикации #{parent['id']}, отмеченной {verdict}" +
                       (f" (копия #{root})" if root != parent["id"] else "")}

    def inherit(self, it: dict, match: dict) -> dict | None:
        """Есть ли у публикации корень «той же истории». Возвращает {kind, root, why, cap, hold} или None."""
        if not self.ctx.settings.story.enabled or not it.get("text") and not it.get("title"):
            return None
        if it.get("id") and self.operator_decided(it):
            return None
        thread = self.thread_root(it, match)
        if thread:
            return thread
        # наш дом/УК в тексте (уровень 3): наследуются только решения оператора и только почти дословной копией
        strict = self.level(match) > 2
        place = self.place(match)
        if not place:
            return None
        t0 = parse_iso(it.get("published_at") or it.get("collected_at") or "")
        cands = [r for r in self.roots(it.get("id") or 0) if r["place"] & place
                 and (not strict or r["kind"] in self.OPERATOR)]
        if t0:
            cands = [r for r in cands if (rt := parse_iso(r["item"].get("published_at") or r["item"].get("collected_at") or ""))
                     is None or abs((t0 - rt).total_seconds()) <= self.days_for(r["kind"]) * 86400]
        from .subject import uk_claim

        body = item_text(it)
        verdict = {"fp": "отмечено «Ложное»", "closed": "отмечено «Решено»"}
        found = None
        for r in sorted(cands, key=lambda r: r["kind"] != "white"):       # белые — первыми: «наше» побеждает
            ok, th, why = self.same_case(it, r["item"])
            if not ok:
                continue
            if r["kind"] == "white":
                return {"kind": "white", "root": r["item"]["id"], "why": f"тема {th:.2f}; " + "; ".join(why)}
            if found is not None:
                continue
            if strict and not ("ссылка на корень" in why or
                               self.copy_ratio(it, r["item"]) >= self.ctx.settings.story.verdict_copy_min):
                continue                                   # наш адрес + лишь общая тема/имя/сумма — не копия
            if r["kind"] in self.OPERATOR and uk_claim(body) and not uk_claim(item_text(r["item"])):
                continue                                   # в копии появилась претензия к УК — это уже другой разговор
            if r["kind"] == "closed" and self.new_keys(match, r["item"]):
                continue                                   # «решённая» история дошла до власти/СМИ — придёт заново
            if r["kind"] == "closed" and RECUR_RE.search(body) and not RECUR_RE.search(item_text(r["item"])):
                continue                                   # «опять/снова» — проблема повторилась, это новость
            blk = bl.inherit_block(body, self.features(r["item"])["tri"], trigrams)
            if blk:
                continue
            found = {"kind": r["kind"], "root": r["item"]["id"], "cap": "P3", "hold": False,
                     "why": "; ".join(x for x in (verdict.get(r["kind"], ""), f"тема {th:.2f}", *why) if x)}
        return found


def audit(ctx: "Ctx", days: int = 30):
    """--case-audit: все пары «склеил бы» за N дней (и почти-пары) в Excel — для разметки и подбора порогов (§5.5)."""
    from openpyxl import Workbook

    from .settings import DATA_DIR
    from .util import msk

    cs = ctx.cases
    rows = [dict(r) for r in ctx.db.q("SELECT * FROM items WHERE analyzed_at>=? AND candidate=1 ORDER BY id", (ago(days=days),))]
    for r in rows:
        r["_m"] = jload(r.get("match_json"), {}) or {}
        r["_p"] = cs.place(r["_m"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Пары"
    ws.append(["N", "R", "склеил бы", "тема", "якоря", "запрет наследования", "решение оператора N", "решение оператора R",
               "N: текст", "R: текст", "N: время", "R: время"])
    n = 0
    for i, a in enumerate(rows):
        for b in rows[:i]:
            if not (a["_p"] & b["_p"]):
                continue
            ok, th, why = cs.same_case(a, b)
            if not ok and not why and th < 0.3:
                continue
            blk = bl.inherit_block(item_text(a), cs.features(b)["tri"], trigrams) if ok else ""
            ws.append([a["id"], b["id"], "да" if ok else "нет", th, "; ".join(why), blk, a.get("workflow"), b.get("workflow"),
                       (a.get("text") or "")[:300], (b.get("text") or "")[:300], msk(a.get("published_at")), msk(b.get("published_at"))])
            n += 1
    path = DATA_DIR / "reports" / f"case_audit_{days}d.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return f"{path} (пар: {n})"
