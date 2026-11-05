"""Анализ «без ИИ» — последняя линия обороны, когда недоступны и Colab, и локальная модель.

Задача не в точности, а в том, чтобы сильный сигнал НЕ ПОТЕРЯЛСЯ: алерт уйдёт с пометкой «без ИИ»,
а когда мозг вернётся — элемент будет переразобран.
"""
from __future__ import annotations

import re

from .matcher import ADDRESSEE_TITLES

TOPIC_WORDS = {
    "отопление": ["отоплен", "батаре", "холодно в квартире", "радиатор"],
    "горячая_вода": ["горячей воды", "горячая вода", "гвс", "кипят"],
    "холодная_вода": ["холодной воды", "нет воды", "хвс", "напор"],
    "электричество": ["свет", "электричеств", "щиток", "искрит"],
    "лифт": ["лифт"],
    "уборка": ["уборк", "грязь", "грязно", "не моют", "дворник"],
    "мусор": ["мусор", "контейнер", "помойк"],
    "снег_наледь": ["снег", "налед", "сосульк", "гололед"],
    "протечки_кровля": ["протеч", "течет", "крыша", "кровл", "затопил"],
    "подвал_канализация": ["подвал", "канализац", "засор", "вонь"],
    "двор_благоустройство": ["двор", "площадк", "газон", "благоустр"],
    "парковка_шлагбаум": ["парков", "шлагбаум", "паркинг"],
    "охрана_доступ": ["охран", "консьерж", "пропуск"],
    "домофон": ["домофон"],
    "тарифы_начисления": ["квитанц", "начислен", "тариф", "переплат", "платеж"],
    "капремонт": ["капремонт", "капитальный ремонт"],
    "грызуны_насекомые": ["крыс", "мыш", "таракан", "клоп"],
    "диспетчерская_связь": ["диспетчер", "не отвечают", "не дозвониться", "аварийк"],
    "безопасность": ["пожар", "газ", "обрушен", "трещин"],
    "смена_УК_ОСС": ["сменить ук", "смена ук", "общее собрание", "осс"],
    "суд_прокуратура_надзор": ["прокурат", "суд", "гжи", "госжилнадзор", "жилинспекц"],
    "благодарность": ["спасибо", "благодар"],
}


EXTRA_WORDS = {"протечки_кровля": ["потоп", "заливает", "залили"], "мусор": ["свалк"],
               "безопасность": ["детонац", "взрыв", "эвакуир"], "суд_прокуратура_надзор": ["надзор", "уголовн", "следствен", "лицензи"],
               "смена_УК_ОСС": ["протокол", "поддельн", "подделк", "фальсифик", "рейдер", "бюллетен", "новая ук", "другую ук"]}
EXCLUDE = {"газ": ("газон", "газет", "газел", "газиров"), "свет": ("светофор", "рассвет", "светл", "светя", "светит"),
           "двор": ("дворец", "дворян"), "мыш": ("мышлен",), "осс": ("россия", "российск"), "протокол": ("протоколы допрос", "протокол об админ", "протокол допрос")}

# Слова, у которых «начало слова» слишком широкое: только перечисленные формы.
# 23.10: «Судя по записи…» давало тему «суд_прокуратура_надзор», а она снимает потолок «не про работу УК».
WORD_FORMS = {
    "суд": re.compile(r"(?<![а-яё])(?:суд(?:а|у|ом|е|ы|ов|ам|ами|ах)?|судебн\w*|судеб|судь[яиеюй]\w*|судей|"
                      r"суд(?:иться|имся|ятся|ится|ился|илась|ились|итесь)|(?:за|от|при)суд\w*|подсуд\w*)(?![а-яё])"),
}


def _topic_hits(text: str) -> list[str]:
    """Темы по словам — с началом слова и исключениями («газон» ≠ газ, «рассвет» ≠ свет, «судя» ≠ суд)."""
    out = []
    for t, ws in TOPIC_WORDS.items():
        for w in ws + EXTRA_WORDS.get(t, []):
            pattern = WORD_FORMS.get(w) or re.compile(r"(?<![а-яё])" + re.escape(w))
            for m in pattern.finditer(text):
                tail = text[m.start(): m.start() + 12]
                if not any(tail.startswith(x) for x in EXCLUDE.get(w, ())):
                    out.append(t)
                    break
            if out and out[-1] == t:
                break
    return out


def topic_hits(text: str) -> list[str]:
    return _topic_hits((text or "").lower())


def confirm_topics(text: str, llm_topics: list[str]) -> list[str]:
    """Тема от модели остаётся, только если её подтверждают слова в тексте; порядок — как у модели."""
    found = _topic_hits((text or "").lower())
    keep = [t for t in dict.fromkeys(llm_topics) if t in found]
    return (keep + [t for t in found if t not in keep])[:4] or ["прочее"]


def analyze(item: dict, match: dict, hint: str = "") -> dict:
    text = f"{item.get('title') or ''} {item.get('text') or ''}".lower()
    hits = match.get("hits", [])
    ours = [h for h in hits if h["kind"] in ("company", "zhk", "address", "group") and h["conf"] >= 0.45]
    neighbor = any(h["kind"] == "neighbor" and h["conf"] >= 0.5 for h in hits) or bool(match.get("foreign"))
    relation = "our" if ours else "neighbor" if neighbor else "cluster" if match.get("clusters") else "none"
    neg, pos = match.get("negativity", 0), match.get("positivity", 0)
    sentiment = -2 if neg >= 0.6 else -1 if neg >= 0.2 else (1 if pos >= 0.3 else 0)
    topics = _topic_hits(text)[:4] or ["прочее"]
    tone = "благодарность" if sentiment > 0 else "жалоба" if sentiment < 0 else "нейтрально"
    addressees = [ADDRESSEE_TITLES.get(a, a) for a in match.get("addressees", [])]
    ent = [{"type": "zhk" if h["kind"] == "zhk" else "uk" if h["kind"] == "company" else h["kind"],
            "name": h["evidence"], "confidence": h["conf"]} for h in ours[:4]]
    where = ", ".join(sorted({h["evidence"].split(" (")[0] for h in ours[:3]})) or ", ".join(match.get("clusters", []))
    return {
        "relevant": relation != "none",
        "relation": relation,
        "entities": ent,
        "sentiment": sentiment,
        "tone": tone,
        "topics": topics,
        "addressees": addressees,
        "author_type": "неизвестно",
        "call_to_action": bool(match.get("cta")),
        "hazard": bool(match.get("hazard")),
        "virality": "низкая",
        "responsibility": "не_определено",     # без ИИ решает текст (monitor/subject.py), а не догадка
        "priority": "P3",
        "summary": f"[без ИИ] {'Негатив' if sentiment < 0 else 'Упоминание'}: {where or 'объект не определён'}; "
                   f"темы: {', '.join(topics)}.",
        "why": hint[:400],
        "action": "Открыть публикацию, проверить факт на объекте, при подтверждении — ответить от имени УК.",
        "reply_draft": "",
        "incident_hint": f"{where[:60]}: {topics[0]}" if where else topics[0],
    }
