"""Риск-скоринг: детерминированная оценка + «правило двух ключей» с вердиктом ИИ.

Два ключа: ИИ не может в одиночку «заглушить» сильный сигнал (наш объект + губернатор + негатив),
а правила в одиночку не поднимают P1, если ИИ доступен и сказал «не про нас» — такой случай уходит
человеку как P3 «проверить».
"""
from __future__ import annotations

import re

from .db import DB
from .settings import Risk
from .util import ago, parse_iso

ORDER = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
GOV = {"gov_spb", "gov_lo", "gov_any", "prosecutor", "gzhi_spb", "gzhn_lo", "zhilkom_spb", "cur", "sk", "rpn"}
AUTH_WORDS = ("губернатор", "гжи", "госжилнадзор", "жилинспекц", "надзор", "прокурат", "следствен", "ск ", "роспотреб", "мчс")

# Массовая авария (для ночных правил: будить ли людей) — тема ЖКХ + признак масштаба
MASS_TOPICS = {"электричество", "отопление", "горячая_вода", "холодная_вода", "протечки_кровля", "безопасность",
               "подвал_канализация"}
MASS_RE = re.compile(r"массов|весь дом|всём доме|всем доме|нескольк\w* дом|во всех|целый дом|целые дома|полрайона|"
                     r"без света|без воды|без отоплен|затопил|затопл|потоп|пожар|эвакуир|эвакуац|запах газа|утечк\w* газа|"
                     r"обрушен|прорыв", re.I)
# Острая ситуация (для P1): массовая авария или отключение, которое длится («четверо суток нет воды»)
OUTAGE_RE = re.compile(r"(?:нет|отключ\w*|отсутств\w*)\s+(?:горяч\w+\s+|холодн\w+\s+)?(?:воды|света|электричеств\w*|отоплени\w*|газа)|"
                       r"(?:\d+|двое|трое|четверо|пятеро|шестеро|несколько|вторые|третьи|четвертые|четвёртые)\s+(?:суток|сутки|дн\w+)\s+без", re.I)


# Хроника («подвал затоплен уже несколько месяцев», «грязная вода больше 3 месяцев») — неприятно, но не «бросай всё»:
# острой её делает только длящееся сейчас отключение («четверо суток нет воды»)
CHRONIC_RE = re.compile(r"месяц|полгода|пол года|годами|уже год|несколько лет|с весны|с лета|с осени|с зимы|постоянно|"
                        r"регулярно|каждую зиму|каждый год", re.I)


def strong_amplifiers(item: dict, match: dict, analysis: dict, source_category: str = "", vel: float = 0.0,
                      wave_n: int = 0, cfg: Risk | None = None) -> list[str]:
    """Что делает негатив про наш объект срочным «≤30 минут» (P1). Охват автора (инфлюенсер, депутат, человек
    на контроле) сам по себе — не причина: он поднимает до P2; P1 — когда к нему добавляется одно из этого."""
    out = []
    body = f"{item.get('title') or ''} {item.get('text') or ''}"
    addressees = set(match.get("addressees") or [])
    llm_addr = " ".join(analysis.get("addressees") or []).lower() + " "
    if addressees & GOV or any(w in llm_addr for w in AUTH_WORDS):
        out.append("власть или надзор")
    if source_category in ("media", "official", "governor") or "media" in addressees \
            or analysis.get("author_type") == "СМИ":
        out.append("СМИ или официальный источник")
    if match.get("cta"):
        out.append("призыв к коллективным действиям")
    if cfg and vel >= cfg.velocity_hot_per_hour / 2:
        out.append("разгоняется")
    topics = set(analysis.get("topics") or [])
    outage = bool(topics & MASS_TOPICS and OUTAGE_RE.search(body))
    mass = bool(topics & MASS_TOPICS and MASS_RE.search(body)) and not CHRONIC_RE.search(body)
    if match.get("hazard") or outage or mass:
        out.append("угроза или массовое/затяжное отключение")
    if wave_n >= 3:
        out.append("волна жалоб")
    return out


def velocity(db: DB, item_id: int) -> float:
    """Вовлечённость в час за последние ~2 часа наблюдений."""
    rows = db.q("SELECT ts, likes, comments, reposts, views FROM metrics WHERE item_id=? AND ts>=? ORDER BY ts",
                (item_id, ago(hours=2)))
    if len(rows) < 2:
        return 0.0
    a, b = rows[0], rows[-1]
    hours = max(0.08, (parse_iso(b["ts"]) - parse_iso(a["ts"])).total_seconds() / 3600)
    d = (b["likes"] - a["likes"]) + 2 * (b["comments"] - a["comments"]) + 3 * (b["reposts"] - a["reposts"]) \
        + (b["views"] - a["views"]) / 100
    return max(0.0, d / hours)


def wave(db: DB, entity_keys: list[str], exclude_id: int) -> int:
    """Сколько негативных упоминаний тех же объектов за 48 ч (признак «волны»)."""
    if not entity_keys:
        return 0
    # только по hits: хиты ЖК, объяснённые чужим домом (explained_hits), волну нашего ЖК не раздувают (дизайн §3.4)
    marks = ",".join("?" for _ in entity_keys)
    return db.scalar(
        "SELECT COUNT(*) FROM items WHERE id<>? AND analyzed_at>=? AND sentiment<0 AND relevant=1 AND match_json IS NOT NULL "
        f"AND EXISTS (SELECT 1 FROM json_each(items.match_json, '$.hits') j WHERE json_extract(j.value, '$.key') IN ({marks}) "
        f"OR json_extract(j.value, '$.zhk') IN ({marks}))",
        [exclude_id, ago(hours=48), *entity_keys, *entity_keys], 0)


def score(item: dict, match: dict, analysis: dict, cfg: Risk, vel: float = 0.0, wave_n: int = 0,
          source_category: str = "") -> tuple[float, list[str]]:
    f: list[str] = []
    rel = analysis.get("relation", "none")
    s = {"our": 40, "neighbor": 22, "cluster": 18}.get(rel, 0)
    if s:
        f.append(f"связь:{rel}+{s}")
    sent = analysis.get("sentiment", 0)
    add = {-2: 25, -1: 12, 0: 0, 1: -10, 2: -15}.get(sent, 0)
    s += add
    if add:
        f.append(f"тональность{sent:+d}:{add:+d}")
    # усилители (власть, охват, скорость, призыв) усиливают НЕГАТИВ; позитивная новость в СМИ — не P2
    amp = 1.0 if sent < 0 else 0.5 if sent == 0 else 0.0
    addressees = set(match.get("addressees", [])) | {a.lower() for a in analysis.get("addressees", [])}
    gov = bool(addressees & GOV) or source_category in ("governor", "official") or any(
        w in " ".join(analysis.get("addressees", [])).lower() for w in ("губернатор", "гжи", "надзор", "прокурат", "следствен", "ск "))

    def amplify(points: float, label: str):
        nonlocal s
        if points * amp:
            s += points * amp
            f.append(f"{label}+{points * amp:.0f}")

    if gov:
        amplify(20, "адресат-власть")
    elif "media" in addressees or source_category == "media":
        amplify(10, "СМИ")
    followers = item.get("author_followers") or 0
    if followers >= cfg.influencer_followers or analysis.get("author_type") in ("инфлюенсер", "СМИ"):
        amplify(15, "охват автора")
    elif followers >= 1000:
        amplify(6, "охват")
    if vel >= cfg.velocity_hot_per_hour:
        amplify(15, f"разгон {vel:.0f}/ч")
    elif vel >= cfg.velocity_hot_per_hour / 2:
        amplify(7, f"рост {vel:.0f}/ч")
    if analysis.get("call_to_action") or match.get("cta"):
        amplify(10, "призыв")
    if analysis.get("hazard") or match.get("hazard"):
        s += 12
        f.append("угроза+12")
    if wave_n >= 3:
        s += 10
        f.append(f"волна({wave_n})+10")
    if rel == "our":
        conf = float(match.get("score", 0.6) or 0.6)
        s *= min(1.0, 0.5 + conf)
    return max(0.0, min(100.0, s)), f


def priority_from_score(s: float, cfg: Risk) -> str:
    if s >= cfg.p1_score:
        return "P1"
    if s >= cfg.p2_score:
        return "P2"
    if s >= cfg.p3_score:
        return "P3"
    return "P4"


def combine(llm_prio: str, score_prio: str, relevant: bool, level: str) -> tuple[str, str]:
    """Итоговый приоритет + пояснение."""
    if level == "rules":
        return score_prio, "без ИИ: по правилам"
    if not relevant:
        if ORDER[score_prio] <= 1:
            return "P3", "ИИ: не про нас, но сигнал сильный → проверить вручную"
        return llm_prio if ORDER[llm_prio] >= 3 else "P4", "ИИ: не про нас"
    best = min(llm_prio, score_prio, key=lambda p: ORDER[p])
    return best, ("ИИ и правила согласны" if llm_prio == score_prio else f"ИИ {llm_prio}, правила {score_prio} → строже")
