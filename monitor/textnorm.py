"""Нормализация русского текста: токены, стемы, леммы, транслит, хэштеги.

Зачем и стем, и лемма: pymorphy3 на незнакомых именах собственных даёт нестабильные леммы
(«Монплезир» → «монплезир», «Монплезире» → «монплезира»), а грубый стем устойчив к склонениям
топонимов и названий ЖК. Для лексиконов (жалобы, ЖКХ) используем лемму, для названий — стем.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

try:
    import pymorphy3

    _MORPH = pymorphy3.MorphAnalyzer()
except Exception:  # pragma: no cover - деградация без словаря
    _MORPH = None

TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#([0-9a-zа-яё_]+)", re.IGNORECASE)

_QUOTES = str.maketrans({"«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
                         "—": "-", "–": "-", "‑": "-", " ": " ", " ": " "})

ENDINGS = sorted(
    """ами ями ого его ому ему ыми ими ях ах ов ев ей ой ый ий ая яя ое ее ую юю ом ем ам ям ых их
    ы и а я у ю е о ь""".split(),
    key=len,
    reverse=True,
)

LAT2CYR = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
           ("yu", "ю"), ("ya", "я"), ("yo", "е"), ("ye", "е"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"),
           ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"),
           ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"),
           ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "и"), ("z", "з"), ("ö", "е"), ("ü", "ю")]


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text).translate(_QUOTES)
    # разметка упоминаний ВК: «[club91135910|Водоканал Санкт-Петербурга]» → «Водоканал Санкт-Петербурга»
    if "|" in t and "[" in t:
        t = _VK_MENTION.sub(r"\1", t)
    return t.replace("ё", "е").replace("Ё", "Е")


_VK_MENTION = re.compile(r"\[(?:id|club|public|event)\d+\|([^\]\n]{1,120})\]")


def translit_lat2cyr(word: str) -> str:
    w = word.lower()
    out, i = [], 0
    while i < len(w):
        for lat, cyr in LAT2CYR:
            if w.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(w[i])
            i += 1
    return "".join(out)


@lru_cache(maxsize=300_000)
def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if w.isdigit() or len(w) <= 4:
        return w
    for e in ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 4:
            return w[: -len(e)]
    return w


@lru_cache(maxsize=300_000)
def lemma(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if _MORPH is None or w.isdigit() or not re.match(r"[а-я]", w):
        return w
    return _MORPH.parse(w)[0].normal_form.replace("ё", "е")


def tokens(text: str) -> list[tuple[str, int, int]]:
    """[(токен_в_нижнем_регистре, start, end)] по нормализованному тексту."""
    return [(m.group(0).lower(), m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def lemmas(text: str) -> list[str]:
    return [lemma(t) for t, _, _ in tokens(normalize(text))]


def lemma_string(text: str) -> str:
    """Строка лемм для полнотекстового поиска (FTS5)."""
    return " ".join(lemmas(text))


def hashtags(text: str) -> list[str]:
    return [h.lower().replace("_", "") for h in HASHTAG_RE.findall(normalize(text))]


def compact(s: str) -> str:
    """«Дом-Мегалит на Неве» → «доммегалитнаневе» (для хэштегов и слитного написания)."""
    return re.sub(r"[^0-9a-zа-я]", "", normalize(s).lower())


@lru_cache(maxsize=20_000)
def adj_genders(word: str) -> frozenset[str]:
    """Роды прилагательного по всем разборам pymorphy3 («Екатерининская» → {femn}); не прилагательное → пусто."""
    w = word.lower().replace("ё", "е")
    if _MORPH is None or not re.match(r"[а-я]", w):
        return frozenset()
    out = set()
    for p in _MORPH.parse(w):
        if p.tag.POS == "ADJF" and p.tag.gender:
            out.add(p.tag.gender)
    return frozenset(out)
