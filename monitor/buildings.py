"""Здания в тексте и «чужой дом в нашем ЖК» (дизайн v7, §2–§3).

Принцип специфичности на замкнутом мире справочника: конкретное здание сильнее названия ЖК. Если текст называет корпус
(строение, литеру, дробь) НАШЕГО ЖЕ ДОМА, которого среди наших нет, название ЖК «объясняется» этим зданием и перестаёт
быть доказательством «нашего». Нечистый или неоднозначный разбор никогда не порождает «чужого»: «не определено» =
«частично» (наше, 0.85). Наш дом, частичный адрес, наша УК/ГК не объясняются никогда.

Здесь же — общие для объяснения и наследования (cases.py) правила текста: предложения, блоки, голос автора,
сравнение/кванторы, самоотнесение.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ разбор номера
LITERA = r"(?P<lit>[а-я])(?![а-я0-9])"
PART = (r"(?:\s*,?\s*лит(?:ера)?\.?\s*(?P<lit2>[а-я])(?![а-я]))?"
        r"(?:\s*,?\s*(?P<pk>к(?:орп(?:ус)?)?\.?|стр(?:оение)?\.?|/|-)\s*(?P<pv>\d{1,3})(?![\d/]))?")
BLD = r"(?P<h>\d{1,3})(?!\d)(?:" + LITERA + r")?" + PART
BLD_RE = re.compile(BLD, re.I)
PAREN_KORP = re.compile(r"\s*\(\s*(?:корпус|корп\.?|к\.?)\s*(\d{1,2})\s*\)", re.I)
# продолжение перечня: «, 13к2», « и к.2», «, корпус 4»
LIST_NEXT = re.compile(r"\s*(?:,|и|,\s*и)\s*(?:(?P<h>\d{1,3})\s*(?P<pk>к(?:орп(?:ус)?)?\.?|стр(?:оение)?\.?)\s*(?P<pv>\d{1,2})"
                       r"|(?P<pk2>к(?:орп(?:ус)?)?\.?|стр(?:оение)?\.?)\s*(?P<pv2>\d{1,2}))(?![\d])", re.I)
ST_TYPES_TEXT = (r"(?:улиц[аеыу]|ул\.?|проспект[аеу]?|пр-т[аеу]?|пр-кт|пр\.?|просп\.?|переулк[аеу]|переул[оа]к|пер\.?|"
                 r"бульвар[аеу]?|б-р|шоссе|ш\.|набережн\w*|наб\.?|проезд[аеу]?|пр-д|алле[еия]|аллея|дорог[еиау]|дор\.?|"
                 r"площад[ьи]|пл\.?|лини[яиюе]|лин\.?)")
AFTER_RE = re.compile(r"^(?:\s*,?\s*" + ST_TYPES_TEXT + r")?\s*,?\s*(?:(?:д|дом)\.?\s*№?\s*|№\s*)?" + BLD, re.I)
ORD_WORDS = {"перв": "1", "втор": "2", "трет": "3", "четверт": "4", "пят": "5", "шест": "6", "седьм": "7", "восьм": "8",
             "девят": "9", "десят": "10"}
_ORD = r"(?:(?P<ow>перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*|(?P<od>\d{1,2})\s*-?\s*(?:го|м|й|ом))"
# «дом №13к1 на Европейском», «в доме 13 корпус 2 по Европейскому», «дома 13к1 и 13к2 на…», «первого корпуса дома 13 на…»,
# «корпуса 1, 2 и 4 дома 13 на…»
BEFORE_RE = re.compile(
    r"(?:(?P<pre>\b(?:дом|дома|доме|домом|домах|д\.)|№)\s*№?\s*" + BLD +
    r"(?P<lst>(?:\s*(?:,|и|,\s*и)\s*(?:\d{1,3}\s*(?:к(?:орп(?:ус)?)?\.?|стр\.?)\s*\d{1,2}|(?:к(?:орп(?:ус)?)?\.?)\s*\d{1,2}))*)"
    r"|" + _ORD + r"\s+корпус\w*\s+(?:дома\s+)?№?\s*(?P<oh>\d{1,3})"
    r"|корпус(?:а|ов|ах)?\s+(?P<kl>\d{1,2}(?:\s*(?:,|и)\s*\d{1,2})+)\s+дома\s+№?\s*(?P<kh>\d{1,3}))"
    r"(?:\s*\(\s*(?:корпус|корп\.?|к\.?)\s*(?P<pp>\d{1,2})\s*\))?"
    r"\s*,?\s*(?:(?:на|по)\s+)?(?:" + ST_TYPES_TEXT + r"\s*)?$", re.I)
STREET_ORD_RE = re.compile(r"(?<![\d/])(\d{1,2})\s*-?\s*(?:й|я|е|ой|ий|ая|го|му|м|ом|ую)\s*$", re.I)
CODE_RE = re.compile(r"(?<![\d/])(\d{1,3})\s*(?:к\.?|корп\.?|корпус)\s*(\d{1,2})(?![\d])", re.I)
LOCATIVE_RE = re.compile(r"(?:\bу|\bвозле|\bоколо|\bнапротив|\bрядом\s+с|\bк|\bот|\bза|\bперед|\bдо|\bво\s+двор\w*|\bв\s+холл\w*|"
                         r"\bна\s+площадк\w*|\bна\s+территори\w*|\bу\s+вход\w*\s+в|\bу\s+подъезд\w*|\bв\s+подъезд\w*)\s+$", re.I)
SUBJECT_RE = re.compile(r"(?:жител\w*|жильц\w*|собственник\w*|инициативн\w+\s+групп\w*|активист\w*)\s+(?:корпуса\s+)?$", re.I)
APT_RE = re.compile(r"\bкв\.?\b|квартир", re.I)

GENDER_TYPES = {"femn": {"ул", "ал", "наб", "лин", "пл", "дор"}, "masc": {"пр", "пр-д", "пер", "б-р"}, "neut": {"ш"}}


@dataclass
class Building:
    num: str
    litera: str = ""
    part: str = ""            # «к», «стр», «/», «-» или ""
    pv: str = ""              # значение части
    clean: bool = True
    start: int = 0
    end: int = 0
    street: str = ""          # название улицы по справочнику
    cls: str = ""             # ours | partial | foreign | other
    zhk: str = ""             # ЖК дома (для «чужого в нашем доме» — сильная связь)
    zhks: set = field(default_factory=set)   # ЖК, чьи наши дома стоят на этой улице
    known: str = ""           # ключ соседа/выученного адреса
    locative: bool = False
    subject: bool = False
    ours_here: list = field(default_factory=list)

    @property
    def label(self) -> str:
        sep = {"к": "к", "стр": " стр. ", "/": "/", "-": "-"}.get(self.part, "")
        return f"{self.street} {self.num}{self.litera}{(sep + self.pv) if self.pv else ''}".strip()


def _part_kind(pk: str | None) -> str:
    if not pk:
        return ""
    pk = pk.lower()
    return "стр" if pk.startswith("стр") else "/" if pk == "/" else "-" if pk == "-" else "к"


def from_match(m: re.Match, base: int, text: str) -> list[Building]:
    """Здание (и перечень) из совпадения BLD/BEFORE/AFTER. base — смещение подстроки в тексте."""
    gd = m.groupdict()
    if gd.get("oh"):                                      # «первого корпуса дома 13»
        ow = (gd.get("ow") or "").lower()
        k = next((v for s, v in ORD_WORDS.items() if ow.startswith(s)), "") if ow else (gd.get("od") or "")
        return [Building(gd["oh"], part="к", pv=k, start=base + m.start(), end=base + m.end())] if k else []
    if gd.get("kh"):                                      # «корпуса 1, 2 и 4 дома 13»
        return [Building(gd["kh"], part="к", pv=v, start=base + m.start(), end=base + m.end())
                for v in re.findall(r"\d{1,2}", gd["kl"])]
    lit = (gd.get("lit") or gd.get("lit2") or "").upper()
    b = Building(gd["h"], litera=lit, part=_part_kind(gd.get("pk")), pv=gd.get("pv") or "",
                 start=base + m.start(), end=base + m.end())
    if b.part == "-":
        tail = text[base + m.end(): base + m.end() + 12]
        if APT_RE.search(text[max(0, base + m.start() - 12): base + m.start()] + tail):
            b.clean = False
    if gd.get("pp") and not b.pv:
        b.part, b.pv = "к", gd["pp"]
    out = [b]
    lst = gd.get("lst") or ""
    if lst:
        for x in re.finditer(r"(?:(\d{1,3})\s*)?(к(?:орп(?:ус)?)?\.?|стр\.?)\s*(\d{1,2})", lst, re.I):
            out.append(Building(x.group(1) or b.num, part=_part_kind(x.group(2)), pv=x.group(3), start=b.start, end=b.end))
    return out


def extend_list(text: str, pos: int, first: Building) -> tuple[list[Building], int]:
    """Продолжение перечня после здания: «13к1, 13к2», «13 к.1 и к.2»."""
    out = []
    while True:
        m = LIST_NEXT.match(text, pos)
        if not m:
            return out, pos
        if m.group("h"):
            out.append(Building(m.group("h"), part=_part_kind(m.group("pk")), pv=m.group("pv"), start=m.start(), end=m.end()))
        else:
            out.append(Building(first.num, litera=first.litera, part=_part_kind(m.group("pk2")), pv=m.group("pv2"),
                                start=m.start(), end=m.end()))
        pos = m.end()


def paren_korpus(text: str, pos: int, b: Building) -> int:
    """«дома 13 на Европейском (корпус 1)» — корпус в скобках после здания/улицы."""
    m = PAREN_KORP.match(text, pos)
    if m and not b.part:
        b.part, b.pv = "к", m.group(1)
        return m.end()
    return pos


def flags(text: str, b: Building) -> None:
    before = text[max(0, b.start - 50): b.start]
    before = re.sub(r"(?:\b(?:дом|дома|доме|домом|д\.|корпус|корпуса|корпусе)\s*)?№?\s*$", "", before, flags=re.I)
    if before and not before.endswith((" ", "\n")):
        before += " "
    b.locative = bool(LOCATIVE_RE.search(before))
    head = re.split(r"[.!?…\n]", text[:b.start])[-1].strip().lower()
    b.subject = bool(SUBJECT_RE.search(before)) or head in ("", "в", "во") or head.endswith(("жители", "жильцы"))


# ------------------------------------------------------------------ класс здания относительно справочника
def classify(b: Building, ours: list, known: list) -> str:
    """ours — наши адреса этой улицы (Address), known — соседи/выученные [(Address, ключ, ЖК)].
    Возвращает ours | partial | foreign | other; заполняет b.zhk, b.known, b.ours_here (§2.3)."""
    same = [a for a in ours if a.num == b.num]
    for a, key, zk in known:
        if a.num == b.num and a.litera == b.litera and (a.korpus or "") == (b.pv or "") and (not b.pv or b.part in (a.part, "-")):
            b.known, b.zhk = key, zk or next((x.zhk for x in same if x.zhk), "")
            b.ours_here = [x.label for x in same]
            if b.part == "-":
                b.part = a.part
            return "foreign"
    if not same:
        return "other"
    b.zhk = next((a.zhk for a in same if a.zhk), "")
    b.ours_here = [a.label for a in same]
    if b.part == "-":                       # «13-1»: корпус, только если M ≤ максимального нашего корпуса этого дома
        mk = max([int(a.korpus) for a in same if a.korpus.isdigit() and a.part == "к"] or [0])
        if b.clean and mk and b.pv.isdigit() and int(b.pv) <= mk:
            b.part = "к"
        else:
            b.clean = False
    if any(a.litera == b.litera and (a.korpus or "") == (b.pv or "") and (not b.pv or a.part == b.part) for a in same):
        return "ours"
    if not b.clean:
        return "partial"
    if any(b.litera in ("", a.litera) and (not b.pv or (a.part == b.part and a.korpus == b.pv)) for a in same):
        return "partial"                    # части опущены: «Европейский 13», «Ручьевский 4» ↔ 4/26, «Чапыгина 3» ↔ 3А
    if b.pv:                                # та же часть с другим значением — чужое; разные формы записи — частично
        if {a.part for a in same} == {b.part} and not b.litera and not any(a.litera for a in same):
            return "foreign"
        return "partial"
    if b.litera and all(a.litera and not a.korpus for a in same):
        return "foreign"                    # «Чапыгина 3Б» при нашем 3А
    return "partial"


# ------------------------------------------------------------------ текст: предложения, блоки, голос автора
_ABBR = r"(?<!\bд)(?<!\bк)(?<!корп)(?<!\bул)(?<!\bпр)(?<!стр)(?<!лит)(?<!\bг)(?<!\bим)(?<!\bпер)(?<!\bнаб)"
SENT_SPLIT = re.compile(_ABBR + r"(?<=[.!?…])\s+|\n+")
LIST_LINE = re.compile(r"^\s*(?:\d{1,2}[.)]\s|[•\-–▪►✅❗🔹🔸📌]|[\U0001F300-\U0001FAFF])", re.M)
QUOTED_RE = re.compile(r"\"[^\"\n]{0,600}\"|“[^”\n]{0,600}”|„[^“\n]{0,600}“")
REPORTED_RE = re.compile(r"\bпо\s+словам\b|\bрассказал\w*|\bговорят\b|\bговорит\b|\bпишут\b|\bсообщают\b|\bжалуются\s*:|"
                         r"\bуверяют\b|\bутверждают\b|\bотмечают\b|\bпризнаются\b|\bделятся\b", re.I)
COMPARISON_RE = re.compile(
    r"\b(?:а|и|но)\s+у\s+нас\b|\bу\s+нас\s+тоже\b|\bкак\s+и\s+(?:у\s+нас|в\s+наш)|в\s+отличие\s+от\s+наш|"
    r"\b(?:весь|всего|всем|всём|по\s+всему)\s+(?:жк|комплекс)|\bвсех\s+(?:жител|собственник|жильц)\w*\s+(?:жк|комплекс)|"
    r"\b(?:все|всех|остальн\w*)\s+(?:корпус|дом|парадн|подъезд)\w*|"
    r"\bто\s+же\s+самое\b|\bтак(?:ая|ой|ие)\s+же\s+(?:ситуаци|истори|проблем)\w*|"
    r"\b(?:в\s+том\s+числе|включая)\s+(?:\S+\s+){0,3}?(?:жильц|жител|дом|корпус|парадн|подъезд|жк)\w*|"
    r"\bтак\s+же,?\s+как\s+(?:и\s+)?(?:у\s+нас|в\s+наш)", re.I)
SELF_REF_RE = re.compile(r"\bу\s+нас\s+(?:в|во|на)\b|\bв\s+наш(?:ем|ей)\s+(?:дом|корпус|парадн|подъезд|двор)\w*|"
                         r"\bнаш\s+(?:дом|корпус)\b|\b(?:нам|мы)\s+тоже\b|\bи\s+у\s+нас\b", re.I)
EVENT_RE = re.compile(r"\bсход\w*|\bмитинг\w*|\bпикет\w*|\bакци\w+\s+протеста|\bсоберемся\b|\bсоберутся\b|\bсобираемся\b|"
                      r"\bсобрались\b|\bприходите\b|\bвстреч\w+\s+(?:жител|с\s)", re.I)
UKCHANGE_RE = re.compile(r"\b(?:смен\w*|сменить|выбра\w*|выбор\w*|нов\w+)\s+(?:ук|управляющ\w*)\b|"
                         r"\bпере(?:йти|ход\w*)\s+(?:в|к)\s+(?:ук|управляющ)|\bрейдер\w*|\bзахват\w*\s+дом", re.I)
VENUE_RE = re.compile(r"\b(?:пройд[её]т|состоится|состоятся|проходит|пройдут)\b", re.I)


def sentences(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in SENT_SPLIT.finditer(text):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(text)))
    return [(a, b) for a, b in out if text[a:b].strip()]


def blocks(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in re.finditer(r"\n+", text):
        out.append((pos, m.start()))
        pos = m.end()
    out.append((pos, len(text)))
    return [(a, b) for a, b in out if text[a:b].strip()]


def author_voice(text: str) -> list[str]:
    """Предложения голосом автора: без текста в кавычках и без предложений с маркерами чужой речи (§3.3)."""
    out = []
    for a, b in sentences(QUOTED_RE.sub(" ", text)):
        s = QUOTED_RE.sub(" ", text)[a:b]
        if not REPORTED_RE.search(s):
            out.append(s)
    return out


def _at(spans: list[tuple[int, int]], pos: int) -> int:
    for i, (a, b) in enumerate(spans):
        if a <= pos <= b:
            return i
    return 0


def explain(text: str, zhk_hits: list, buildings: list[Building], topic_hits) -> tuple[set, dict, bool]:
    """Какие ЖК объяснены чужим зданием (§3.1–§3.3). zhk_hits — [(key, start, end)] (позиция 0,0 — неизвестна, хэштег).
    Возвращает (объяснённые ключи, {ключ: метка здания}, была ли блокировка сравнением)."""
    sents, blks = sentences(text), blocks(text)
    is_list = len(LIST_LINE.findall(text)) >= 2
    sent_of = lambda pos: sents[_at(sents, pos)] if sents else (0, len(text))
    explained, labels, blocked = set(), {}, False
    for z in dict.fromkeys(k for k, _, _ in zhk_hits):
        own = [b for b in buildings if z in b.zhks or b.zhk == z]
        expl = [b for b in own if b.cls == "foreign" and b.zhk == z and not b.locative]
        if not expl or any(b.cls != "foreign" for b in own if not b.locative):
            continue
        ok = True
        for _, zs, ze in (h for h in zhk_hits if h[0] == z):
            zb = _at(blks, zs)
            scope = []
            for b in expl:
                bb = _at(blks, b.start)
                if bb == zb or _address_only(text, blks[bb]):
                    scope.append(b)
                    continue
                if is_list:
                    continue
                if len(blks) >= 3 and 0 not in (zb, bb):
                    tz = set(topic_hits(text[blks[zb][0]:blks[zb][1]].lower())) - {"благодарность"}
                    tb = set(topic_hits(text[blks[bb][0]:blks[bb][1]].lower())) - {"благодарность"}
                    if tz and tb and not tz & tb:
                        continue
                scope.append(b)
            if not scope:
                ok = False
                break
            sa, sb = sent_of(zs)
            seg = text[sa:sb]
            subj = [b for b in expl if sa <= b.start <= sb and b.subject]
            venue = bool(VENUE_RE.search(seg)) and bool(EVENT_RE.search(seg))
            if (EVENT_RE.search(seg) or UKCHANGE_RE.search(seg)) and (not subj or venue):
                ok = False
                break
            for a, e in {sent_of(zs)} | {sent_of(b.start) for b in scope}:
                if COMPARISON_RE.search(_strip_attributed(text, a, e, scope)):
                    ok, blocked = False, True
                    break
            if not ok:
                break
        if ok:
            explained.add(z)
            labels[z] = ", ".join(dict.fromkeys(b.label for b in expl))
    return explained, labels, blocked


def _address_only(text: str, blk: tuple[int, int]) -> bool:
    """Адресная строка: «📍 Европейский пр., 13к1», «Адрес: …» — не больше 6 слов."""
    return len(re.findall(r"[0-9a-zа-я]+", text[blk[0]:blk[1]], re.I)) <= 6


def _strip_attributed(text: str, a: int, e: int, blds: list[Building]) -> str:
    """Речь, явно приписанная чужому дому («жители дома X: …», «по словам жильцов дома X, …»), не блокирует (§3.3)."""
    seg = text[a:e]
    for b in blds:
        if a <= b.start <= e and b.subject:
            rest = text[b.end:e]
            if re.match(r"\s*[:\-]", rest) or re.search(r"по\s+словам\s+(?:\w+\s+){0,2}$", text[a:b.start], re.I):
                return text[a:b.start]
    return seg


def inherit_block(n_text: str, root_trigrams: set, trigrams) -> str:
    """Запрет наследования (§5.3): сравнение/кванторы — во всех собственных предложениях N (и в пересказе);
    самоотнесение — только голосом автора. Собственные = триграммы входят в корень меньше чем на 0.5."""
    def own(s: str) -> bool:
        st = trigrams(s)
        return not (st and len(st & root_trigrams) / len(st) >= 0.5)
    for a, b in sentences(n_text):
        s = n_text[a:b]
        if own(s):
            m = COMPARISON_RE.search(s)
            if m:
                return m.group(0)
    for s in author_voice(n_text):
        if own(s):
            m = SELF_REF_RE.search(s)
            if m:
                return m.group(0)
    return ""
