"""Конвейер: сбор → матчинг → очередь ИИ → риск → инцидент → теги → алерт."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING

from . import risk, rules
from .textnorm import lemma_string, normalize
from .util import ago, iso, jdump, jload, msk, now_utc, parse_iso, sha1

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger("radar.pipeline")

LEVEL_NAMES = {"colab": "ИИ Colab", "local": "локальный ИИ", "rules": "правила"}
PLATFORM_TITLES = {"vk_post": "ВК-пост", "vk_comment": "ВК-комментарий", "tg": "Telegram", "rss": "СМИ",
                   "gnews": "СМИ (агрегатор)", "web": "сайт", "manual": "ручной ввод"}



def strip_signature(text: str, *names: str | None) -> str:
    """Убирает короткие строки-подписи с названием канала/паблика: «🌇 Жизнь в Кудрово ( ВК | Max )»."""
    keys = [n.split(" — ")[0].strip().lower() for n in names if n and len(n.strip()) >= 5]
    if not keys:
        return text
    return "\n".join(l for l in text.split("\n") if not (len(l) < 100 and any(k in l.lower() for k in keys)))


def too_old(published_at: str | None, days: int) -> bool:
    from .util import parse_iso
    dt = parse_iso(published_at) if published_at else None
    return bool(dt and days and (now_utc() - dt).days >= days)

# Происшествия (полиция/охрана) и претензии к УК — в monitor/subject.py; имена оставлены здесь для совместимости
from .subject import CRIME_RE, HOUSING_SUPERVISION, NOT_UK, UK_DUTY_RE, third_party, uk_claim  # noqa: E402

# Наш ЖК/адрес только как ориентир: потерялся или нашёлся питомец, потерянные вещи, объявления. К работе УК и
# застройщика отношения не имеет — P4, даже если модель «увидела» безопасность жильцов (29.09: кот у ЖК Виктория → P2).
_ANIMAL = (r"(?<![а-яё])(?:кот|кота|коту|котом|коты|котов|котик\w*|котейк\w*|котён\w*|котен\w*|котят\w*|кошк\w*|кошечк\w*|"
           r"пёс|пес|пса|псу|псом|пёсик\w*|песик\w*|собак\w*|собачк\w*|щен\w*|попуга\w*|хомяк\w*|кролик\w*|питом\w*|животн\w*)(?![а-яё])")
_LOSTFOUND = (r"потерял\w*|потеряш\w*|пропал\w*|сбежал\w*|убежал\w*|нашл\w*|найд\w*|нашел\w*|нашёл\w*|ищ(?:у|ем|ет|ут)\b|"
              r"хозя\w*|добрые руки|пристро\w*|передерж\w*|приют\w*|бездомн\w*|видели|встреча\w*|поймать|подкармл\w*")
_THINGS = (r"ключ\w*|кошел\w*|кошёл\w*|телефон\w*|паспорт\w*|сумк\w*|рюкзак\w*|документ\w*|перчатк\w*|шапк\w*|очки|"
           r"самокат\w*|велосипед\w*|игрушк\w*|серьг\w*|сережк\w*|кольц\w*|банковск\w* карт\w*")
_LOST = r"потерял\w*|потеряли|обронил\w*|нашл\w*|найден\w*|нашел|нашёл|забыл\w*"
ANIMAL_RE = re.compile(_ANIMAL, re.I)
LOSTFOUND_RE = re.compile(_LOSTFOUND, re.I)
THINGS_RE = re.compile(rf"(?:{_LOST})[^.!?\n]{{0,60}}(?:{_THINGS})|(?:{_THINGS})[^.!?\n]{{0,40}}(?:{_LOST})", re.I)
# только недвусмысленные слова объявлений: «услуги», «сниму (на видео)» встречаются и в жалобах
ADS_RE = re.compile(r"(?<![а-яё])(?:продам|продаю|прода[её]тся|продаются|сдам|сдаю|сда[её]тся|куплю|отдам|отдаю|"
                    r"отда[её]тся|маникюр\w*|репетитор\w*|вакансия|подработк\w*|промокод\w*|розыгрыш\w*)(?![а-яё])", re.I)
# Что делает тему «нашей» даже без жалобы: надзор, суд, смена УК, авария, опасность — это поднимать можно
ESC_TOPICS = {"смена_УК_ОСС", "суд_прокуратура_надзор", "авария", "безопасность"}
NEG_TONES = {"жалоба", "угроза", "сарказм"}


def landmark_only(body: str) -> str:
    """Какой «не наш» сюжет в тексте (питомец, потеря вещей, объявление) или ''."""
    if ANIMAL_RE.search(body) and LOSTFOUND_RE.search(body):
        return "потерялся или нашёлся питомец"
    if THINGS_RE.search(body):
        return "потеря или находка вещей"
    if ADS_RE.search(body):
        return "объявление"
    return ""


ESCALATION_ADDR = {"gov_spb", "gov_lo", "gov_any", "prosecutor", "gzhi_spb", "gzhn_lo", "zhilkom_spb", "sk", "rpn",
                   "license", "media", "onf", "deputy", "cur"}


def own_escalation(match: dict) -> bool:
    """Жалоба в собственной группе УК выходит наружу: адресат — власть/надзор/СМИ, или призыв к соседям."""
    return bool(match.get("cta")) or bool(ESCALATION_ADDR & set(match.get("addressees") or []))


# «во всём ЖК», «все корпуса» — в общем ЖК это включает и наши дома
ZHK_WIDE_RE = re.compile(r"(?:весь|всего|всем|всём|по\s+всему)\s+(?:жк|комплекс\w*|квартал\w*|микрорайон\w*)|"
                         r"(?:все|всех|во\s+всех)\s+(?:корпус\w*|дом(?:а|ов|ах)\b|парадн\w*|подъезд\w*)", re.I)


class Pipeline:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    def ours_strength(self, match: dict, text: str = "") -> tuple[str, list[str]]:
        """Насколько текст доказывает «наш объект»: ('strong', …) — наша УК/ГК, наш адрес или ЖК, где все дома наши;
        ('shared', [ЖК]) — назван только ЖК, где есть дома других УК (shared в справочнике), без нашего дома и УК;
        ('', []) — нашего нет. Общий ЖК + «во всём ЖК / все корпуса» — strong: это и про наши дома."""
        cfg, reg = self.ctx.settings.risk, self.ctx.reg
        hits = [h for h in (match.get("hits") or []) if h["conf"] >= cfg.min_match_conf]
        if any(h["kind"] in ("company", "group", "address") for h in hits):
            return "strong", []
        zh = [h.get("zhk") or h["key"] for h in hits if h["kind"] == "zhk"]
        if not zh:
            return "", []
        if any(not reg.zhk_shared(z) for z in zh) or (text and ZHK_WIDE_RE.search(text)):
            return "strong", []
        return "shared", [reg.zhk_name(z) for z in dict.fromkeys(zh)]

    # ------------------------------------------------------------------ приём
    def ingest(self, items: list[dict], source: dict) -> dict:
        ctx, db = self.ctx, self.ctx.db
        st = {"new": 0, "edited": 0, "cand": 0, "urgent": 0}
        watch = ctx.watch
        for raw in items:
            text = normalize(raw.get("text") or "")
            title = normalize(raw.get("title") or "")
            if not (text or title):
                continue
            if watch.ignored(raw, text):
                continue
            it = {
                "platform": raw["platform"], "external_id": str(raw["external_id"]),
                "source_key": source.get("key"), "url": raw.get("url"),
                "author_id": raw.get("author_id"), "author_name": raw.get("author_name"),
                "author_followers": int(raw.get("author_followers") or 0),
                "parent_external_id": raw.get("parent_external_id"), "parent_text": raw.get("parent_text"),
                "title": title, "text": text, "published_at": raw.get("published_at") or iso(),
                "collected_at": iso(), "likes": raw.get("likes", 0) or 0, "comments": raw.get("comments", 0) or 0,
                "reposts": raw.get("reposts", 0) or 0, "views": raw.get("views", 0) or 0,
                "media_json": jdump(raw.get("media") or []), "content_hash": sha1(title, text),
            }
            iid, kind = db.upsert_item(it)
            if kind == "same":
                self._metrics(iid, it)
                continue
            st[kind] += 1
            # подпись канала («🌇 Жизнь в Кудрово (ВК | Max)») — не упоминание района: убираем перед сопоставлением
            m = ctx.matcher.match(strip_signature(text, source.get("title"), it.get("author_name")), title,
                                  source.get("category") or "", source.get("region") or "",
                                  context_zhks=self.parent_zhks(raw.get("parent_text")))
            if source.get("category") == "own":
                # собственные группы/каналы УК: посты УК — контекст для ИИ; жалоба жителя в комментариях — сигнал,
                # но УК видит её сама, поэтому будим людей только при угрозе огласки (губернатор, прокуратура, СМИ, призыв)
                resident = raw["platform"] in ("vk_post", "vk_comment") and not str(raw.get("author_id") or "").startswith("vk:-")
                if resident and (m.negativity > 0 or m.hazard or m.cta or m.addressees):
                    m.candidate, m.urgency = True, int(own_escalation(m.to_json()))
                    m.reason = (m.reason + ", жалоба в нашей группе").strip(", ")
                else:
                    m.candidate, m.urgency = False, 0
            watched = watch.author_watched(it)
            if watched:
                m.candidate, m.reason = True, (m.reason + ", автор на контроле").strip(", ")
                if m.negativity > 0 or m.housing:
                    m.urgency = 1
            # срок давности — для всех, и для людей на контроле: при добавлении человека или источника приходит его
            # история за месяцы (30.09: посты Хмелёвой от 26.08 и 07.09 ушли алертами)
            if m.candidate and too_old(it["published_at"], ctx.settings.schedule.max_age_days):
                m.candidate, m.urgency = False, 0         # старый пост из первого опроса — в архив, не в анализ
                m.reason = (m.reason + ", старше срока — в архив").strip(", ")
            hint = ctx.matcher.hint(m) + (f" | автор на контроле: {watched}" if watched else "")
            db.x("UPDATE items SET candidate=?, match_json=?, match_score=?, match_hint=? WHERE id=?",
                 (int(m.candidate), jdump(m.to_json()), m.score, hint, iid))
            db.fts_put(iid, lemma_string(f"{title} {text}"))
            if m.candidate:
                st["cand"] += 1
                st["urgent"] += m.urgency
                if raw["platform"] in ("vk_post", "vk_comment", "tg"):
                    db.x("UPDATE items SET track_until=? WHERE id=?",
                         (iso(now_utc() + timedelta(hours=ctx.settings.schedule.track_hours)), iid))
                self._metrics(iid, it)
                if kind == "edited":
                    db.event("info", "pipeline", f"публикация отредактирована: #{iid}")
                db.enqueue(iid, m.urgency)
                ctx.memory.touch_author({**it, "id": iid})
        return st

    def parent_zhks(self, parent_text: str | None) -> list[str]:
        """ЖК родительского поста (включая объяснённые) — для кодов корпусов в комментарии: «в 13к2 тоже…» (§2.1)."""
        if not parent_text:
            return []
        from .cases import Cases

        return sorted(Cases.place(self.ctx.matcher.match(parent_text).to_json()))

    def _metrics(self, iid: int, it: dict) -> None:
        """Замер динамики — только если цифры изменились или прошло 30 минут (иначе 288 одинаковых строк в сутки)."""
        db = self.ctx.db
        r = db.one("SELECT track_until FROM items WHERE id=?", (iid,))
        if not (r and r["track_until"] and r["track_until"] > iso()):
            return
        vals = (it["likes"], it["comments"], it["reposts"], it["views"])
        last = db.one("SELECT ts, likes, comments, reposts, views FROM metrics WHERE item_id=? ORDER BY ts DESC LIMIT 1", (iid,))
        if last and tuple(last[k] for k in ("likes", "comments", "reposts", "views")) == vals and last["ts"] > ago(minutes=30):
            return
        db.x("INSERT INTO metrics(item_id, ts, likes, comments, reposts, views) VALUES(?,?,?,?,?,?)", (iid, iso(), *vals))

    # ------------------------------------------------------------------ итог анализа
    def finalize(self, item_id: int, analysis: dict, level: str, notify: bool = True) -> dict | None:
        ctx, db = self.ctx, self.ctx.db
        item = db.item(item_id)
        if not item:
            return None
        match = jload(item["match_json"], {}) or {}
        src = db.one("SELECT * FROM sources WHERE key=?", (item["source_key"],))
        src_cat = src["category"] if src else ""
        analysis = self.guard_relation(item, match, analysis, src_cat)
        vel = risk.velocity(db, item_id)
        keys = sorted({h["key"] for h in match.get("hits", []) if h["conf"] >= 0.45} |
                      {h.get("zhk") for h in match.get("hits", []) if h.get("zhk")} - {"", None})
        wave_n = risk.wave(db, keys, item_id)
        score, factors = risk.score(item, match, analysis, ctx.settings.risk, vel, wave_n, src_cat)
        sp = risk.priority_from_score(score, ctx.settings.risk)
        prio, why = risk.combine(analysis.get("priority", "P3"), sp, analysis.get("relevant", False), level)
        analysis = {**analysis, "topics": rules.confirm_topics(f"{item.get('title') or ''} {item.get('text') or ''}",
                                                                analysis.get("topics", []))}
        cap, evidence, meta = self.evidence_cap_ex(item, match, analysis)
        if risk.ORDER.get(prio, 4) < risk.ORDER.get(cap, 4):
            why += f" → не выше {cap}: {evidence}"
            prio = cap
        keys = risk.strong_amplifiers(item, match, analysis, src_cat, vel, wave_n, ctx.settings.risk)
        if prio == "P1" and not keys and item["platform"] != "manual":
            # «два ключа»: негатив про наш объект + власть/СМИ/призыв/угроза/разгон. Охват автора (инфлюенсер, депутат,
            # человек на контроле) без этого — P2, «в течение 2 часов», а не «бросай всё» (30.09: будничные жалобы
            # депутатов и активистов приходили как P1)
            why += " → P2: для P1 нужен второй ключ (власть/надзор, СМИ, призыв, угроза или затяжная авария, разгон)"
            prio = "P2"
        if item["workflow"] == "false_positive":
            prio = "P4"
        tags = self._tags(item, match, analysis, level, prio)
        incident_id = self._incident(item, match, analysis, prio) if (
            analysis.get("relevant") and prio != "P4" and cap == "P1") else None
        analysis = {**analysis, "_risk": {"score": round(score, 1), "factors": factors, "velocity": round(vel, 1),
                                          "wave": wave_n, "combine": why, "level": level, "keys": keys, **meta}}
        prev = item["priority"]
        db.x("UPDATE items SET analysis_json=?, analysis_level=?, relevant=?, sentiment=?, priority=?, risk_score=?, "
             "tags=?, summary=?, incident_id=?, status='analyzed', analyzed_at=?, lease_owner=NULL, lease_until=NULL "
             "WHERE id=?",
             (jdump(analysis), level, int(bool(analysis.get("relevant"))), analysis.get("sentiment", 0), prio, score,
              jdump(tags), analysis.get("summary", ""), incident_id, iso(), item_id))
        if analysis.get("sentiment", 0) < 0 and item.get("author_id") and not item.get("analyzed_at"):
            db.x("UPDATE authors SET n_negative=n_negative+1 WHERE key=?", (item["author_id"],))   # только первый разбор
        escalated = prev and risk.ORDER.get(prio, 4) < risk.ORDER.get(prev, 4)
        instant = ctx.settings.alerts.instant_priorities
        if prev in instant and prio not in instant and db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (item_id,)):
            ctx.alerts.schedule_refresh(item_id, "dropped", f"пересмотрено ({LEVEL_NAMES.get(level, level)}): {prio}, {why}")
        if prio == "P3" and analysis.get("relevant") and not prev:
            twin = self.same_story_alert(item_id, tags)       # волна по открытому инциденту — счётчик в его карточке
            if twin and (db.kv_get(f"furef:{twin}") or "") < ago(minutes=10):
                db.kv_set(f"furef:{twin}", iso())
                ctx.alerts.schedule_refresh(twin)
        root = meta.get("copy_of")
        if root and (db.kv_get(f"furef:{root}") or "") < ago(minutes=2) and db.one("SELECT 1 FROM alerts WHERE item_id=?", (root,)):
            db.kv_set(f"furef:{root}", iso())             # счётчик «📎 ещё N копий» в карточке корня
            ctx.alerts.schedule_refresh(root)
        if notify and root and meta.get("copy_kind") == "white" and prio in ctx.settings.alerts.instant_priorities \
                and db.one("SELECT 1 FROM alerts WHERE item_id=?", (root,)) and self.copy_merges(root, keys):
            # копия истории, которую оператор отметил «наше»: своей карточки нет, строка в карточке корня (§5.3)
            db.event("info", "alerts", f"#{item_id} ({prio}) — копия истории #{root} (наше): без отдельной карточки")
            return {"priority": prio, "score": score, "tags": tags}
        if notify and prio in ctx.settings.alerts.instant_priorities and item["workflow"] not in ("false_positive", "resolved"):
            if not prev or escalated or not db.one("SELECT 1 FROM alerts WHERE item_id=?", (item_id,)):
                twin = self.same_story_alert(item_id, tags)
                if twin and prio == "P1" and not self.merges_into(twin, keys):
                    twin = None                                    # поднялось на новый уровень — это новость
                if twin:
                    # та же тема в том же районе, первый алерт ещё открыт: не будим людей второй раз, а дописываем
                    src_t = db.one("SELECT title FROM sources WHERE key=?", (item["source_key"],))
                    note = f"ещё публикация по теме: {src_t['title'] if src_t else item['source_key']}, {msk(item['published_at'])}"
                    db.event("info", "alerts", f"#{item_id} ({prio}) присоединён к открытому инциденту #{twin} — без нового алерта")
                    ctx.alerts.schedule_refresh(twin, None, note)
                elif self.is_late(item):
                    # новый источник или человек, перерыв в работе: находки за прошлые часы — одной сводкой, без эскалаций
                    ctx.alerts.to_digest(item_id, prio)
                else:
                    ctx.alerts.raise_alert(item_id, prio, update=bool(escalated))
        return {"priority": prio, "score": score, "tags": tags}

    def object_evidence(self, item: dict, match: dict, src_cat: str = "") -> bool:
        """Есть ли в тексте (или в посте, к которому комментарий) наша УК, ЖК или адрес — то, что даёт право сказать
        «наш объект». Улица без номера дома, район, сосед — не дают."""
        cfg = self.ctx.settings.risk
        if item.get("platform") == "manual" or src_cat == "own":
            return True
        body = f"{item.get('title') or ''} {item.get('text') or ''}"
        if self.ours_strength(match, body)[0] == "strong":
            return True
        cases = getattr(self.ctx, "cases", None)
        if item.get("id") and cases and cases.white(item["id"]):
            return True                                   # оператор сказал «✅ Наше» / «🏠 Наш дом»
        parent = item.get("parent_text") or ""
        return bool(parent and self.ours_strength(self.ctx.matcher.match(parent).to_json(), parent)[0] == "strong")

    def guard_relation(self, item: dict, match: dict, analysis: dict, src_cat: str = "") -> dict:
        """Модель не может «назначить» публикацию нашей: relation=our без нашего объекта в тексте исправляется ДО риска,
        а её «Чем опасно» и черновик ответа от УК не показываются. 23.10: пост СК про «дом на проспекте Обуховской
        Обороны» модель назвала «нашим ЖК Дом-Мегалит на Неве» — по подсказке «улица → ЖК»; номера дома в тексте нет."""
        if analysis.get("relation") != "our" or self.object_evidence(item, match, src_cat):
            return analysis
        if "автор на контроле" in (match.get("reason") or ""):
            return analysis            # человек на контроле пишет «у нас опять…» без адреса — его дом нам известен
        from .brain.core import DEFAULT_ACTION

        hits = match.get("hits", []) or []
        streets = [str(h.get("evidence", "")).replace("улица:", "").split(" — ")[0].strip()
                   for h in hits if h["kind"] == "street"]
        neighbor = bool(match.get("neighbor") or match.get("foreign") or match.get("explained_hits") or
                        any(h["kind"] == "neighbor" for h in hits))
        rel = "neighbor" if neighbor else "cluster" if (streets or match.get("clusters")) else "none"
        shared = self.ours_strength(match)[1]
        if not shared and item.get("parent_text"):
            shared = self.ours_strength(self.ctx.matcher.match(item["parent_text"]).to_json())[1]
        if shared and rel == "none":
            rel = "cluster"                   # наш ЖК назван, но он общий: это «наш район», а не «не про нас»
        what = (f"назван только ЖК {', '.join(shared)}, где есть дома других УК; нашего дома и нашей УК нет" if shared else
                f"названа только улица ({', '.join(dict.fromkeys(streets))}), номера нашего дома нет" if streets else
                "назван соседний дом или ЖК" if neighbor else "назван только район" if match.get("clusters") else
                "наших УК, ЖК и адресов нет")
        prio = analysis.get("priority", "P3")
        return {**analysis, "relation": rel, "relevant": rel != "none",
                "priority": "P3" if prio in ("P1", "P2") and rel != "none" else ("P4" if rel == "none" else prio),
                "why": (f"В тексте {what}: дом может быть не наш, без адреса это не повод отвечать от лица УК."
                        if shared else f"В тексте {what}: это не наш объект (ИИ ошибочно отнёс публикацию к нашему ЖК/УК)."),
                "action": DEFAULT_ACTION["P3"] if rel != "none" else "", "reply_draft": "",
                "_relation_fixed": f"our→{rel}"}

    def is_late(self, item: dict) -> bool:
        """Опубликовано заметно раньше, чем Радар это разобрал: при подключении источника или человека приходит его
        история, после перерыва — всё накопившееся. Отдельный алерт на каждую такую находку — это и есть «террор»."""
        h = self.ctx.settings.alerts.late_hours
        pub = parse_iso(item.get("published_at")) if item.get("published_at") else None
        return bool(h and pub and item.get("platform") != "manual" and (now_utc() - pub).total_seconds() > h * 3600)

    @staticmethod
    def story_key(tags: list[str]) -> tuple[str, str] | None:
        cl = next((t.split(":", 1)[1] for t in tags if t.startswith("кластер:")), None) or \
            next((t.split(":", 1)[1] for t in tags if t.startswith("ЖК:")), None)
        tp = next((t.split(":", 1)[1] for t in tags if t.startswith("тема:") and t != "тема:прочее"), None)
        return (cl, tp) if cl and tp else None

    @staticmethod
    def story_sig(tags: list[str]) -> tuple[set, set]:
        places = {t.split(":", 1)[1] for t in tags if t.startswith(("кластер:", "ЖК:"))}
        topics = {t.split(":", 1)[1] for t in tags if t.startswith("тема:") and t != "тема:прочее"}
        return places, topics

    @classmethod
    def same_story(cls, a: list[str], b: list[str]) -> bool:
        """Одна история: то же место (ЖК или район) и хотя бы одна общая тема. Порядок тем не важен:
        «ГВС, ХВС» у одного автора и «ХВС, ГВС» у другого — одно отключение."""
        pa, ta = cls.story_sig(a)
        pb, tb = cls.story_sig(b)
        return bool(pa & pb and ta & tb)

    def merges_into(self, twin_id: int, keys: list[str]) -> bool:
        """Новый P1 по открытому инциденту: без нового «ключа» — дописываем в карточку; появилась власть/СМИ/призыв,
        которых у инцидента не было, или инцидент был P2 — отдельное сообщение (история поднялась на уровень)."""
        t = self.ctx.db.item(twin_id) or {}
        if t.get("priority") != "P1":
            return False
        had = set(((jload(t.get("analysis_json"), {}) or {}).get("_risk") or {}).get("keys") or [])
        return set(keys) <= had | {"волна жалоб", "разгоняется"}

    def copy_merges(self, root_id: int, keys: list[str]) -> bool:
        """Копия «нашей» истории без нового ключа эскалации (власть, СМИ, призыв, которых у корня не было) —
        дописывается в карточку корня; с новым ключом — история поднялась на уровень, отдельная карточка."""
        t = self.ctx.db.item(root_id) or {}
        had = set(((jload(t.get("analysis_json"), {}) or {}).get("_risk") or {}).get("keys") or [])
        return set(keys) <= had | {"волна жалоб", "разгоняется"}

    def same_story_alert(self, item_id: int, tags: list[str]) -> int | None:
        """Открытый алерт за 12 ч по той же теме в том же районе (волна публикаций про одно отключение)."""
        places, topics = self.story_sig(tags)
        if not (places and topics):
            return None
        for r in self.ctx.db.q("SELECT a.item_id, i.tags FROM alerts a JOIN items i ON i.id=a.item_id WHERE a.created_at>? "
                               "AND a.item_id<>? AND i.workflow IN ('open','in_work') ORDER BY a.created_at",
                               (ago(hours=12), item_id)):
            if self.same_story(tags, jload(r["tags"], []) or []):
                return r["item_id"]
        return None

    def evidence_cap(self, item: dict, match: dict, analysis: dict) -> tuple[str, str]:
        """Потолок приоритета = строже из двух: «есть ли в тексте наш объект» и «о том ли речь, за что отвечает УК»."""
        cap, why, _ = self.evidence_cap_ex(item, match, analysis)
        return cap, why

    def evidence_cap_ex(self, item: dict, match: dict, analysis: dict) -> tuple[str, str, dict]:
        """То же + происхождение потолка: cap_kind (foreign | registry_check | same_case), hold (ждать утра),
        copy_of / copy_kind (корень «той же истории»)."""
        self._meta: dict = {}
        cases = getattr(self.ctx, "cases", None)
        if item.get("id") and match.get("explained_hits") and cases and cases.white(item["id"]):
            # оператор сказал «✅ Наше»: потолок без объяснения чужим домом (дизайн §6)
            match = {**match, "hits": (match.get("hits") or []) + match["explained_hits"], "explained_hits": []}
        cap, why = self.object_cap(item, match, analysis)
        meta = dict(self._meta)
        if item.get("platform") == "manual":
            return cap, why, meta
        sub = self.subject_cap(item, match, analysis)
        if sub and risk.ORDER.get(sub[0], 4) > risk.ORDER.get(cap, 4):
            cap, why = sub
            meta.pop("hold", None)
        inh = cases.inherit(item, match) if cases else None
        if inh:
            meta.update({"copy_of": inh["root"], "copy_kind": inh["kind"]})
            if inh["kind"] != "white" and risk.ORDER.get(cap, 4) < risk.ORDER.get(inh["cap"], 4):
                cap, why = inh["cap"], f"та же история, что #{inh['root']} ({inh['why']})"
                meta.update({"cap_kind": "same_case", "hold": False})
        return cap, why, meta

    def subject_cap(self, item: dict, match: dict, analysis: dict) -> tuple[str, str] | None:
        """Наш объект назван, но тема не наша.
        • ЖК только как ориентир (кот, ключи, «продам») и ни слова о ЖКХ, УК, надзоре, опасности → P4, никому.
        • Ни жалобы, ни угрозы, ни надзора (нейтральное упоминание, анонс, новость) → не выше P3, в отчёт.
        Срочность — это проблема, за которую спросят с УК или застройщика, а не просто название ЖК в тексте."""
        body = f"{item.get('title') or ''} {item.get('text') or ''}"
        low = body.lower()
        housing = [t for t in rules.topic_hits(low) if t != "благодарность"]
        addr = set(match.get("addressees") or [])
        gov = bool(addr & ESCALATION_ADDR)
        hazard = bool(match.get("hazard"))
        duty = uk_claim(body)
        kind = landmark_only(low)
        if kind and not (housing or duty or gov or hazard):
            return "P4", f"не про работу УК ({kind}) — ЖК упомянут только как ориентир"
        # Действия третьих лиц (хулиганство, травля, конфликт соседей) без претензии к УК — не PR-авария УК (23.10:
        # «подростки забрасывают дверь пенсионеров яйцами», ЖК Виктория 9к2 — четыре P2 подряд). Губернатор или полиция
        # в адресатах этого не меняют: это всё равно не работа УК; меняет претензия к УК, жилнадзор или угроза в доме.
        # Публикации СМИ и власти не трогаем — как и для происшествий (object_cap): там вопрос репутации ЖК.
        src = self.ctx.db.one("SELECT category FROM sources WHERE key=?", (item.get("source_key"),))
        press = bool(src and src["category"] in ("media", "official", "governor"))
        supervision = bool(addr & HOUSING_SUPERVISION)
        tp = third_party(body)
        if tp and not (press or duty or supervision or hazard):
            return "P3", f"{tp}: зона полиции и жильцов, претензии к УК в тексте нет — в отчёт"
        resp = analysis.get("responsibility")
        if resp in NOT_UK and not (press or duty or supervision or hazard or housing):
            return "P3", f"ИИ: это зона {NOT_UK[resp]}, а не УК; претензии к УК и темы ЖКХ в тексте нет — в отчёт"
        negative = (analysis.get("sentiment", 0) or 0) < 0 or analysis.get("tone") in NEG_TONES \
            or float(match.get("negativity") or 0) >= 0.2
        if not (negative or hazard or gov or ESC_TOPICS & set(housing)):
            return "P3", "нет ни жалобы, ни угрозы, ни надзора — не срочно"
        return None

    def object_cap(self, item: dict, match: dict, analysis: dict) -> tuple[str, str]:
        """Потолок приоритета по тому, что реально написано: наш объект/адрес → без ограничений;
        наша улица без дома → P3 (P2 — только на «плотных» улицах risk.street_dense); только район или сосед → P3;
        ничего нашего → P4 (фон).
        Модель может рассуждать «может спровоцировать волну» — будить людей это не должно."""
        cfg = self.ctx.settings.risk
        hits = match.get("hits", []) or []
        if item.get("platform") == "manual":          # переслал оператор — он уже решил, что это важно
            return "P1", "прислано оператором"
        src = self.ctx.db.one("SELECT category FROM sources WHERE key=?", (item.get("source_key"),))
        body = f"{item.get('title') or ''} {item.get('text') or ''}"
        if (not src or src["category"] not in ("media", "official", "governor")) and CRIME_RE.search(body) \
                and not UK_DUTY_RE.search(body):
            # «мужчина с разбитой головой у лифта»: разбираются охрана и полиция, не PR — в отчёт, без алерта
            return "P3", "происшествие (полиция/охрана), к работе УК не относится"
        if src and src["category"] == "own":
            if own_escalation(match):
                return "P2", "жалоба в нашей группе + угроза огласки (власть, надзор, СМИ, призыв)"
            return "P3", "жалоба в нашей группе — УК видит её сама"
        strength, shared = self.ours_strength(match, body)
        if strength == "strong":
            return "P1", "наш объект в тексте"
        if shared:
            return cfg.shared_zhk_cap, (f"ЖК {', '.join(shared)}: там есть дома других УК, а наш дом и наша УК в тексте "
                                        "не названы")
        explained = match.get("explained_hits") or []
        if explained:
            # название ЖК объяснено чужим корпусом нашего дома (дизайн §4.2): потолок — менее срочный из «как раньше»
            # (по сырым хитам) и нового; P2 «проверить» поэтому возникает только там, где раньше был P1
            fs = [f for f in match.get("foreign") or [] if f.get("zhk")]
            known = any(f.get("known") for f in fs) or any(self.ctx.reg.zhk_complete(f["zhk"]) for f in fs)
            label = ", ".join(dict.fromkeys(h.get("explained_by", "") for h in explained))
            zname = ", ".join(dict.fromkeys(self.ctx.reg.zhk_name(h.get("zhk") or h["key"]) for h in explained))
            ours = ", ".join(dict.fromkeys(x for f in fs for x in f.get("ours_here", [])))[:120]
            if known:
                new, why, kind = cfg.foreign_cap, f"чужой дом в нашем ЖК {zname}: {label} — не наш (наши: {ours})", "foreign"
            else:
                new, why, kind = (cfg.registry_check_cap, f"ЖК {zname}: дом {label} не из справочника (наши: {ours}) — "
                                  f"проверить, наш ли дом", "registry_check")
            old, old_why = self.object_cap(item, {**match, "hits": hits + explained, "explained_hits": []}, analysis)
            if risk.ORDER.get(old, 4) > risk.ORDER.get(new, 4):
                return old, old_why
            self._meta = {"cap_kind": kind, "hold": kind == "registry_check" and new == "P2"}
            return new, why
        parent = item.get("parent_text") or ""
        if parent:
            pm = self.ctx.matcher.match(parent).to_json()
            ps, pshared = self.ours_strength(pm, parent)
            if ps == "strong":
                return "P1", "наш объект в исходном посте или ветке"
            if pshared:
                return cfg.shared_zhk_cap, (f"в исходном посте только ЖК {', '.join(pshared)}, где есть дома других УК; "
                                            "наш дом и наша УК не названы")
        streets = [h for h in hits if h["kind"] == "street" and str(h.get("evidence", "")).startswith("улица:")]
        bare = [h for h in streets if h["conf"] >= 0.3]       # 0.35 — дом не назван
        if bare:
            from .textnorm import compact

            dense = {compact(s) for s in cfg.street_dense}
            name = str(bare[0].get("evidence", "")).replace("улица:", "").strip()
            if any(h.get("key") in dense for h in bare):
                return cfg.street_dense_cap, f"улица {name}: почти все дома наши (risk.street_dense), номер дома не назван"
            return cfg.street_cap, (f"улица {name} без номера дома — на ней есть наш дом, но и много чужих; "
                                    "это район, а не наш объект")
        if streets:                                           # 0.25 — назван другой дом на нашей улице
            return cfg.cluster_cap, "наша улица, но другой дом"
        if "автор на контроле" in (match.get("reason") or ""):
            return cfg.watched_cap, "автор на контроле, наш адрес не назван"
        if match.get("clusters") or match.get("neighbor"):
            return cfg.cluster_cap, "только район/сосед, наших адресов нет"
        if analysis.get("relation") == "our":        # модель уверена, но в тексте нечем подтвердить — пусть глянет человек
            return "P3", "ИИ считает нашим, но объект/адрес в тексте не найден — проверить"
        return cfg.no_evidence_cap, "в тексте нет наших объектов, адресов и районов"

    def rematch_recent(self, hours: int = 72) -> int:
        """После обновления сопоставителя: пересобрать совпадения и приоритеты за последние дни (без новых вызовов ИИ).
        Убирает старые ошибки из отчётов: «этаже» ≠ УК «Этажи», подпись «Жизнь в Кудрово» ≠ район, тёзки из Удомли.
        Понизившиеся срочные — в self.dropped (их карточки свернёт startup); каждое повышение пишется в журнал с
        причиной (проверка монотонности на реальной базе, дизайн §5.5)."""
        db, n = self.ctx.db, 0
        self.dropped: list[int] = []
        instant = self.ctx.settings.alerts.instant_priorities
        for it in db.q("SELECT * FROM items WHERE analyzed_at>? AND priority IN ('P1','P2','P3')", (ago(hours=hours),)):
            it = dict(it)
            before = it["priority"]
            self._rematch_one(it)
            after = (db.item(it["id"]) or {}).get("priority")
            self._requeue_if_stale(it)
            if before in instant and after not in instant:
                self.dropped.append(it["id"])
            elif risk.ORDER.get(after, 4) < risk.ORDER.get(before, 4):
                m = jload((db.item(it["id"]) or {}).get("match_json"), {}) or {}
                ours = [h["evidence"] for h in m.get("hits", []) if h["kind"] == "address" and h["conf"] >= 0.45]
                db.event("info", "pipeline", f"rematch: #{it['id']} повышен {before}→{after}; причина: "
                                             f"{'; '.join(ours) or 'не объяснено новым нашим зданием — проверить'}")
            n += 1
        return n

    def _requeue_if_stale(self, it: dict) -> None:
        """ИИ разобрал публикацию до пересмотра и счёл её «нашей», а теперь это чужой дом или копия истории: тексты
        «Чем опасно / Что делать» и риск устарели — отдаём на повторный разбор с новой подсказкой (без алертов: потолок
        не изменится). Без ИИ (уровень «правила») тексты шаблонные — повторять нечего."""
        cur = self.ctx.db.item(it["id"]) or {}
        a = jload(cur.get("analysis_json"), {}) or {}
        m = jload(cur.get("match_json"), {}) or {}
        ours = [h for h in m.get("hits", []) if h["kind"] in ("company", "zhk", "group", "address") and h["conf"] >= 0.45]
        said_ours = a.get("relation") == "our" or str(a.get("_relation_fixed") or "").startswith("our→")
        if cur.get("analysis_level") in ("colab", "local") and said_ours and not cur.get("parent_text") and \
                ((a.get("_risk") or {}).get("cap_kind") in ("foreign", "registry_check", "same_case") or
                 (m.get("foreign") and not ours)):
            self.ctx.db.enqueue(it["id"], 0)

    def _rematch_one(self, it: dict, notify: bool = False) -> None:
        db = self.ctx.db
        src = db.one("SELECT * FROM sources WHERE key=?", (it["source_key"],))
        src = dict(src) if src else {}
        m = self.ctx.matcher.match(strip_signature(it["text"] or "", src.get("title"), it.get("author_name")),
                                   it.get("title") or "", src.get("category") or "", src.get("region") or "",
                                   context_zhks=self.parent_zhks(it.get("parent_text")))
        if too_old(it["published_at"], self.ctx.settings.schedule.max_age_days) and it["platform"] != "manual":
            m.candidate = False
        db.x("UPDATE items SET match_json=?, match_score=?, candidate=?, match_hint=? WHERE id=?",
             (jdump(m.to_json()), m.score, int(m.candidate or it["platform"] == "manual"), self.ctx.matcher.hint(m), it["id"]))
        if getattr(self.ctx, "cases", None):
            self.ctx.cases.invalidate()
        if not m.candidate and it["platform"] != "manual":
            db.x("UPDATE items SET priority='P4', relevant=0 WHERE id=?", (it["id"],))
        else:
            a = jload(it["analysis_json"], {}) or {}
            # пересчёт старого — без новых алертов: только понижения (карточки поправятся сами)
            self.finalize(it["id"], a, it["analysis_level"] or "rules", notify=notify)

    def recap_recent(self, hours: int = 72) -> list[int]:
        """После обновления правил: срочные за последние дни, которые теперь не проходят «потолок», — понизить
        (без рассылки). Так «Горячее 24ч» и эскалации сразу очищаются от старого шума."""
        return self.reconcile_down(hours, refresh=False) + list(getattr(self, "dropped", []))

    def reconcile_down(self, hours: int | None = None, refresh: bool = True) -> list[int]:
        """Сверщик, направление «вниз» (дизайн §5.4), раз в минуту: открытые P1/P2 без решения оператора, чей потолок
        теперь ниже (появился корень «той же истории», чужой дом, правка справочника), — понизить и свернуть карточки.
        Только понижает; повторный прогон без изменений ничего не делает."""
        db, ids = self.ctx.db, []
        hours = hours or self.ctx.settings.story.window_hours
        instant = self.ctx.settings.alerts.instant_priorities
        for it in db.q("SELECT * FROM items WHERE priority IN ('P1','P2') AND analyzed_at>? AND workflow='open'",
                       (ago(hours=hours),)):
            it = dict(it)
            if self.ctx.cases.white(it["id"]):
                continue
            cap, why, meta = self.evidence_cap_ex(it, jload(it["match_json"], {}) or {}, jload(it["analysis_json"], {}) or {})
            if risk.ORDER.get(it["priority"], 4) >= risk.ORDER.get(cap, 4):
                continue
            a = jload(it["analysis_json"], {}) or {}
            r = {**(a.get("_risk") or {}), **meta}
            r["combine"] = (r.get("combine") or "") + f" → сверщик: не выше {cap}: {why}"
            db.x("UPDATE items SET priority=?, analysis_json=? WHERE id=?", (cap, jdump({**a, "_risk": r}), it["id"]))
            db.x("UPDATE alerts SET priority=? WHERE item_id=?", (cap, it["id"]))
            db.event("info", "pipeline", f"сверщик: #{it['id']} {it['priority']}→{cap}: {why}")
            ids.append(it["id"])
            if not refresh:
                continue
            if cap in instant:
                # P1 → P2 «проверить»: отправленные карточки правятся на месте; если карточка ещё в очереди — её
                # сбросит still_relevant (приоритет понижен), а P2 ставится заново по ночным правилам
                if not db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (it["id"],)) and not db.one(
                        "SELECT 1 FROM outbox WHERE item_id=? AND status IN ('sent','relay','relay_leased') "
                        "AND dedup_key LIKE 'tg:%'", (it["id"],)):
                    self.ctx.alerts.raise_alert(it["id"], cap)
                else:
                    self.ctx.alerts.schedule_refresh(it["id"], None, f"понижено: {why}")
            else:
                self.ctx.alerts.schedule_refresh(it["id"], "dropped", why)
        if ids:
            db.event("info", "pipeline", f"сверщик: понижено срочных: {len(ids)}")
        return ids

    def reconcile_up(self, reason: str) -> list[int]:
        """Сверщик, направление «вверх» (§5.4) — только по явным событиям: правка справочника, «🏠 Наш дом», «✅ Наше»,
        «↩️ Вернуть». Публикации за root_days, подавленные чужим домом или «той же историей», без решения оператора —
        пересобрать и пересчитать; стало срочнее — обычная доставка (копии «нашей» истории — строкой в карточке корня)."""
        db, ids = self.ctx.db, []
        days = self.ctx.settings.story.root_days
        for it in db.q("SELECT * FROM items WHERE analyzed_at>? AND workflow='open' AND (match_json LIKE '%\"foreign\": [{%' "
                       "OR analysis_json LIKE '%\"cap_kind\"%' OR analysis_json LIKE '%\"copy_of\"%')", (ago(days=days),)):
            it = dict(it)
            before = it["priority"]
            self._rematch_one(it, notify=True)
            after = (db.item(it["id"]) or {}).get("priority")
            if risk.ORDER.get(after, 4) < risk.ORDER.get(before, 4):
                ids.append(it["id"])
                db.event("info", "pipeline", f"пересмотр ({reason}): #{it['id']} {before}→{after}")
        return ids

    def rules_analyze(self, item_id: int) -> dict | None:
        item = self.ctx.db.item(item_id)
        if not item:
            return None
        a = rules.analyze(item, jload(item["match_json"], {}) or {}, item.get("match_hint") or "")
        return self.finalize(item_id, a, "rules")

    # ------------------------------------------------------------------ теги и инциденты
    def _tags(self, item: dict, match: dict, a: dict, level: str, prio: str) -> list[str]:
        reg, tags = self.ctx.reg, []
        src = self.ctx.db.one("SELECT category, meta_json FROM sources WHERE key=?", (item.get("source_key"),))
        if src and src["category"] == "own":        # комментарий в группе ЖК — он про этот ЖК, даже если название не написано
            meta = jload(src["meta_json"], {}) or {}
            if meta.get("zhk"):
                tags.append(f"ЖК:{reg.zhk_name(meta['zhk'])}")
            if meta.get("company"):
                tags.append(f"УК:{reg.company_name(meta['company']).replace('ООО ', '')}")
        # комментарий без названия объекта («подвал под детсадом затоплен») под постом про наш ЖК — объект берём из поста,
        # иначе в карточке «объект не определён» и сюжет не склеивается
        if not any(h["kind"] in ("company", "zhk", "address", "group") and h["conf"] >= 0.45 for h in match.get("hits", [])) \
                and item.get("parent_text"):
            pm = self.ctx.matcher.match(item["parent_text"]).to_json()
            match = {**match, "hits": (match.get("hits") or []) + (pm.get("hits") or []),
                     "clusters": list(dict.fromkeys((match.get("clusters") or []) + (pm.get("clusters") or [])))}
        # ЖК «Виктория» обслуживают две УК; если в тексте есть точный адрес — берём УК по адресу
        addr_cos = {h["company"] for h in match.get("hits", []) if h["kind"] == "address" and h["conf"] >= 0.8 and h.get("company")}
        shared_only = self.ours_strength(match, f"{item.get('title') or ''} {item.get('text') or ''}")[0] == "shared"
        for h in match.get("hits", []):
            if h["conf"] < 0.45:
                continue
            if h.get("zhk"):
                tags.append(f"ЖК:{reg.zhk_name(h['zhk'])}")
            if shared_only and h["kind"] == "zhk":
                continue                  # общий ЖК без нашего дома: чья УК — неизвестно, «УК:» в карточку не пишем
            cos = [c for c in (h.get("company") or "").split(",") if c]
            if h["kind"] == "zhk" and addr_cos & set(cos):
                cos = sorted(addr_cos & set(cos))
            for c in cos:
                tags.append(f"УК:{reg.company_name(c).replace('ООО ', '')}")
            if h["kind"] == "neighbor":
                n = next((x for x in reg.neighbors if x["key"] == h["key"]), None)
                tags.append(f"сосед:{n['name'] if n else h['key']}")
        cl = {c["key"]: c["name"] for c in reg.clusters}
        tags += [f"кластер:{cl.get(c, c).split(' (')[0]}" for c in match.get("clusters", [])]
        tags += [f"тема:{t}" for t in dict.fromkeys(a.get("topics", []))]
        tags += [f"тон:{a.get('tone', 'нейтрально')}", f"площадка:{PLATFORM_TITLES.get(item['platform'], item['platform'])}"]
        tags += [f"адресат:{x}" for x in a.get("addressees", [])[:4]]
        tags += [f"автор:{a.get('author_type', 'неизвестно')}", f"приоритет:{prio}"]
        if a.get("call_to_action"):
            tags.append("призыв")
        if a.get("hazard"):
            tags.append("угроза")
        if a.get("virality") == "высокая":
            tags.append("вирусность:высокая")
        if level == "rules":
            tags.append("без_ИИ")
        seen, out = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _incident(self, item: dict, match: dict, a: dict, prio: str) -> int:
        db = self.ctx.db
        hits = sorted((h for h in match.get("hits", []) if h["conf"] >= 0.45), key=lambda h: -h["conf"])
        ek = next((h["zhk"] for h in hits if h.get("zhk")), None) or next(
            (h["key"] for h in hits if h["kind"] in ("company", "neighbor")), None) or (
            match.get("clusters") or ["прочее"])[0]
        topic = (a.get("topics") or ["прочее"])[0]
        row = db.one("SELECT * FROM incidents WHERE entity_key=? AND topic=? AND status='open' AND last_seen>=? "
                     "ORDER BY last_seen DESC LIMIT 1", (ek, topic, ago(hours=72)))
        if row:
            mp = min(row["max_priority"] or "P4", prio, key=lambda p: risk.ORDER.get(p, 4))
            db.x("UPDATE incidents SET last_seen=?, n_items=n_items+1, max_priority=? WHERE id=?", (iso(), mp, row["id"]))
            return row["id"]
        title = a.get("incident_hint") or f"{ek}: {topic}"
        return db.x("INSERT INTO incidents(title, entity_key, topic, first_seen, last_seen, n_items, max_priority) "
                    "VALUES(?,?,?,?,?,1,?)", (title, ek, topic, iso(), iso(), prio))
