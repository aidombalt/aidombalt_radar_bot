"""Матчер: находит наши УК/ЖК/адреса, соседей и гео-кластеры в «грязном» тексте.

Принцип «фильтр из неидеальных условий» (урок кейса Родимцевой):
  • название может быть в любом падеже, с опечаткой, в хэштеге, латиницей;
  • названия многозначны (театр «Балтийский дом», имя «Виктория», город «Москва») — нужен контекст;
  • человек пишет не «ООО Приморский Дом», а «управляйка на Авиаторов Балтики 9»;
  • пост может быть вообще не про нас, а про соседей через дорогу — и всё равно ударить по нам;
  • адресат важнее текста: жалоба в комментариях губернатору ≠ жалоба в домовом чате.
Матчер не выносит вердикт — он отбирает кандидатов и даёт ИИ подсказки (hint). Вердикт — за ИИ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from rapidfuzz import fuzz

from . import buildings as bl
from .entities import STREET_TYPE_RE, STREET_TYPES, Registry, parse_address
from .textnorm import adj_genders, compact, hashtags, lemma, normalize, stem, tokens, translit_lat2cyr

# ------------------------------------------------------------------ лексиконы (леммы)
HOUSING = set("""ук управляйка управляшка жкх жк диспетчер диспетчерская аварийка квитанция начисление тариф
отопление батарея радиатор гвс хвс вода кипяток лифт подъезд парадная двор дворник уборка мусор мусоропровод
контейнер протечка течь крыша кровля подвал фасад домофон шлагбаум консьерж охрана паркинг парковка капремонт
собственник жилец жилищный управляющий инспекция жилинспекция госжилнадзор общедомовой одпу счетчик
электричество свет щиток наледь сосулька снег газон детский площадка благоустройство""".split())

NEGATIVE = set("""ужас кошмар позор беспредел безобразие отвратительно отвратительный хамство хамский бездействие
безответственность жалоба пожаловаться жаловаться прокуратура суд иск помогите невозможно антисанитария вонь
грязь грязный затопить затопление течь холодно холодный плесень крыса таракан обман обманывать воровать мошенник
побор игнорировать игнор бардак разруха развалиться сломать сломанный неделя месяц бесполезно бесит издевательство
надоесть достать отписка отписываться халтура некомпетентный наплевательский возмутительно возмущение
разобраться проверка штраф лицензия отозвать обнаглеть наглый наглость хамить свинство издеваться
тупой бестолковый отвратный жуть треш катастрофический катастрофа недовольный недовольство нарушение
убитый аварийный""".split())

HOUSING_RE = re.compile(r"управляющ\w*\s+компани|коммунальщик|коммунальн\w+\s+услуг|жилищн\w+\s+(?:инспекц|комитет|надзор)")

NEG_PHRASES = ["сколько можно", "не работает", "не отвечает", "не отвечают", "нет воды", "нет света", "нет отопления",
               "примите меры", "прошу разобраться", "просим разобраться", "до сих пор", "никто не", "не реагирует",
               "не реагируют", "третий день", "вторую неделю", "уже месяц", "лишить лицензии", "сменить ук",
               "сменить управляющую", "плохая ук", "ужасная ук", "худшая ук", "поддельн", "подделали подпис",
               "подделка подпис", "фальсификац", "рейдерск", "захват дома", "сфальсифицир"]

POSITIVE = set("спасибо благодарность благодарить молодец оперативно отлично приятно чисто уютно".split())

# «пишите» — только как призыв к массовым действиям; «напишите мне», «пишите в личку» (потерялся кот, объявления) — нет
CTA_PISHITE = re.compile(r"(?<![а-яё])пишите(?!\s+(?:мне|нам в личк|в личк|в лс|в личные|в директ|в вотсап|в whatsapp|в тг|лично))")
CTA_PHRASES = ["жалуйтесь", "давайте все", "подпишите", "подписывайте", "репост", "распространите",
               "собираемся", "коллективн", "петици", "пишем губернатору", "пишем в", "все в прокуратуру",
               "массово", "объединяемся", "общее собрание", "смена ук", "поднимем шум", "в сми", "максимальный репост",
               "прошу репост", "репостами", "размещайте", "помогите соседям", "помочь своим соседям", "массовость",
               "общественный резонанс", "видеообращение", "видеообращени", "эффект долиной"]

HAZARD = ["пожар", "задымлен", "обрушен", "обрушил", "затопил", "прорыв", "запах газа", "утечка газа", "взрыв",
          "застрял в лифте", "застряли в лифте", "упал лифт", "эвакуир", "рухнул", "без света", "без воды", "без отопления", "кипяток",
          "сосульк", "наледь", "упала плитка", "трещина", "искрит", "электрощит", "травм", "ребенок упал", "опасно для детей", "угроза жизни"]

ADDRESSEES = {
    "gov_spb": ["беглов", "a_beglov", "александр дмитриевич", "губернатор петербурга", "губернатор спб", "смольный"],
    "gov_lo": ["дрозденко", "drozdenko_a", "drozdenko_au_lo", "александр юрьевич", "губернатор ленобласти", "губернатор ло",
               "губернатор ленинградской", "губернатору области", "губернатор области"],
    "gov_any": ["губернатор", "губер", "губернатору"],
    "gzhi_spb": ["гжи", "жилинспекция", "жилищная инспекция", "жилищную инспекцию", "жилищной инспекции"],
    "gzhn_lo": ["госжилнадзор", "жилнадзор", "gilnadzor_lenoblast", "gilnadzor_lenobl47", "комитет госжилнадзора", "плешаков"],
    "zhilkom_spb": ["жилищный комитет", "жилкомитет", "жилком", "удод"],
    "prosecutor": ["прокуратур", "прокурор", "lenobl_prokuratura", "внесла представление", "внесено представление"],
    "sk": ["sledcomru", "следственный комитет", "следственного комитета", "ск россии", "бастрыкин", "правоохранительн",
           "поручил доложить", "уголовное дело", "уголовного дела", "sledcom_press", "infocentrskrf"],
    "license": ["реестр лицензий", "реестра лицензий", "лишить лицензии", "лишение лицензии", "лишили лицензии",
                "отзыв лицензии", "аннулировании лицензии", "аннулировать лицензию"],
    "rpn": ["роспотребнадзор", "rospotrebnadzor"],
    "admin": ["администрация района", "глава администрации", "администрация мурино", "глава района", "администрацию"],
    "deputy": ["депутат", "заксобрание", "законодательное собрание", "мундеп"],
    "cur": ["цур", "центр управления регионом"],
    "media": ["фонтанка", "журналист", "в сми", "телеканал", "78.ru", "мойка78", "mash", "в редакцию", "47news"],
    "onf": ["народный фронт", "народного фронта", "народному фронту", "onf_front", "nf_spb"],
}
ADDRESSEE_TITLES = {
    "gov_spb": "губернатор СПб", "gov_lo": "губернатор ЛО", "gov_any": "губернатор", "gzhi_spb": "ГЖИ СПб",
    "gzhn_lo": "Госжилнадзор ЛО", "zhilkom_spb": "Жилищный комитет", "prosecutor": "прокуратура",
    "admin": "администрация", "deputy": "депутаты", "cur": "ЦУР", "media": "СМИ", "sk": "Следственный комитет",
    "rpn": "Роспотребнадзор", "license": "лицензия УК", "onf": "Народный фронт",
}

AFTER_TYPE_RE = re.compile(r"^\s*,?\s*(?:" + "|".join(sorted(map(re.escape, STREET_TYPES), key=len, reverse=True))
                           + r")[а-я]*\.?", re.I)
PREFIX_TOKENS = {"жк", "ук", "ооо", "комплекс", "комплексе", "комплекса", "ао", "тсж", "управляющая", "управляющей"}
NUM_AFTER_STREET = re.compile(
    r"^[\s,.]*(?:(?:д|дом)\.?\s*)?(\d{1,3})\s*([а-е](?![а-я]))?(?:\s*(?:,\s*)?(?:к(?:орп(?:ус)?)?\.?|/|стр\.?|-)\s*(\d{1,2}))?",
    re.I,
)



# Упоминание наших регионов (для названий-«тёзок»): город, область, районы присутствия
REGION_RE = re.compile(r"спб|петербург|питер|ленобласт|ленинградск|\bло\b|всеволожск|мурин|кудров|девяткин|"
                       r"сертолов|приморск|выборгск|невск|красногвардейск|фрунзенск|московск|кировск|калининск|"
                       r"адмиралтейск|петроградск|василеостровск|колпин|пушкин|парнас|шушар", re.I)

@dataclass
class Hit:
    kind: str                     # company | zhk | group | address | street | neighbor | cluster
    key: str
    conf: float
    evidence: str
    company: str = ""
    zhk: str = ""
    start: int = 0
    end: int = 0
    explained_by: str = ""        # хит ЖК объяснён чужим зданием (дизайн §3): «Европейский 13к1»

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("kind", "key", "conf", "evidence", "company", "zhk")}
        if self.explained_by:
            d["explained_by"] = self.explained_by
        return d


@dataclass
class Match:
    hits: list[Hit] = field(default_factory=list)
    clusters: list[str] = field(default_factory=list)
    housing: int = 0
    negativity: float = 0.0
    positivity: float = 0.0
    addressees: list[str] = field(default_factory=list)
    cta: bool = False
    hazard: list[str] = field(default_factory=list)
    candidate: bool = False
    urgency: int = 0
    score: float = 0.0
    neighbor: bool = False
    reason: str = ""
    # «чужой дом в нашем ЖК» (дизайн §3.4): объяснённые хиты ЖК — отдельно, старые потребители читают только hits
    explained_hits: list[Hit] = field(default_factory=list)
    foreign: list[dict] = field(default_factory=list)
    raw_level: int = 0            # 3 наш дом/УК/ГК, 2 ЖК или частичный адрес, 1 улица/кластер, 0 — ничего (до объяснения)
    blocked: bool = False         # в тексте сравнение/включение нас («во всём ЖК», «а у нас»)
    partial: bool = False         # есть частичный адрес (основной номер наш, части опущены/иначе записаны)
    buildings: list = field(default_factory=list)

    @property
    def ours(self) -> list[Hit]:
        return [h for h in self.hits if h.kind in ("company", "zhk", "group", "address") and h.conf >= 0.45]

    def companies(self) -> list[str]:
        return sorted({h.company for h in self.ours if h.company})

    def zhks(self) -> list[str]:
        return sorted({h.zhk for h in self.ours if h.zhk})

    def to_json(self) -> dict:
        return {
            "hits": [h.as_dict() for h in self.hits], "clusters": self.clusters, "housing": self.housing,
            "negativity": round(self.negativity, 2), "positivity": round(self.positivity, 2),
            "addressees": self.addressees, "cta": self.cta, "hazard": self.hazard,
            "neighbor": self.neighbor, "score": round(self.score, 2), "reason": self.reason,
            "explained_hits": [h.as_dict() for h in self.explained_hits], "foreign": self.foreign,
            "raw_level": self.raw_level, "blocked": self.blocked, "partial": self.partial,
        }


@dataclass
class _Alias:
    stems: tuple[str, ...]
    surface: tuple[str, ...]
    kind: str
    key: str
    company: str
    zhk: str
    ambiguous: bool
    prefix_required: bool
    surface_only: bool
    negative: tuple[str, ...]
    text: str
    hard_negative: tuple[str, ...] = ()   # «дом-центр», «калининград»: другая организация даже с «УК …» перед названием


class Matcher:
    def __init__(self, reg: Registry, extra_keywords: Iterable[str] = ()):
        self.reg = reg
        self.aliases: dict[str, list[_Alias]] = {}
        # компании с «общими» названиями (есть тёзки по всей России) — засчитываются только с привязкой к СПб/ЛО
        self.generic_companies = {k for k, c in reg.companies.items() if c.get("ambiguous") or c.get("generic_name")}
        self.multi: list[_Alias] = []
        self.compacts: dict[str, _Alias] = {}
        ctx = reg.context.get("housing", [])
        self.ctx_lemmas = {lemma(w) for c in ctx for w in c.split()} | HOUSING
        self._build_streets()
        self._build()
        self.cluster_kw = {
            c["key"]: ([tuple(stem(t) for t, _, _ in tokens(normalize(k))) for k in c.get("keywords", [])],
                       [tuple(stem(t) for t, _, _ in tokens(normalize(s))) for s in c.get("streets", [])])
            for c in reg.clusters
        }
        self.extra = [tuple(stem(t) for t, _, _ in tokens(normalize(k))) for k in extra_keywords]

    # ---------------------------------------------------------------- индекс
    def _add(self, text: str, kind: str, key: str, company: str = "", zhk: str = "", ambiguous=False,
             prefix_required=False, surface_only=False, negative: Iterable[str] = (), hard_negative: Iterable[str] = ()):
        variants = {text}
        if re.search(r"[a-zA-Z]", text):
            variants.add(translit_lat2cyr(text))
        for v in variants:
            toks = [t for t, _, _ in tokens(normalize(v))]
            if not toks:
                continue
            stems = tuple(stem(t) for t in toks)
            if kind in ("zhk", "company") and stems in self.streets:
                prefix_required = True     # название совпадает с нашей улицей → только «ЖК …»/«УК …»
            a = _Alias(stems, tuple(toks), kind, key, company, zhk, ambiguous,
                       prefix_required, surface_only, tuple(normalize(n).lower() for n in negative), text,
                       tuple(normalize(n).lower() for n in hard_negative))
            self.aliases.setdefault(a.stems[0], []).append(a)
            if len(toks) >= 2 and len(text) >= 10:
                self.multi.append(a)
            self.compacts[compact(v)] = a

    def _build(self):
        g = self.reg.raw.get("group", {})
        for al in g.get("aliases", []):
            self._add(al, "group", "group", ambiguous=True, negative=["театр", "спектакль", "фестиваль"],
                      hard_negative=g.get("hard_negative", []))
        for ck, c in self.reg.companies.items():
            so = set(c.get("surface_only", []))
            for al in c.get("aliases", []):
                amb = c.get("ambiguous", False) and not al.upper().startswith(("УК ", "ООО "))
                self._add(al, "company", ck, company=ck, ambiguous=amb, surface_only=al in so,
                          prefix_required=amb and c.get("prefix_required", False), negative=c.get("negative", []),
                          hard_negative=c.get("hard_negative", []))
        for zk, z in self.reg.zhks.items():
            comps = sorted({a.company for a in self.reg.addresses if a.zhk == zk}) or [
                c["key"] for c in self.reg.raw.get("companies", []) for o in c.get("objects", []) if o.get("zhk") == zk]
            so = set(z.get("surface_only", []))
            for al in z.get("aliases", []):
                self._add(al, "zhk", zk, company=",".join(comps), zhk=zk, ambiguous=z.get("ambiguous", False),
                          prefix_required=z.get("prefix_required", False), surface_only=al in so,
                          negative=z.get("negative", []), hard_negative=z.get("hard_negative", []))
        for n in self.reg.neighbors:
            so = set(n.get("surface_only", []))
            for al in n.get("aliases", []):
                self._add(al, "neighbor", n["key"], surface_only=al in so, negative=n.get("negative", []))

    def _build_streets(self):
        self.streets: dict[tuple[str, ...], list[tuple]] = {}
        def street_seq(name: str) -> tuple[str, ...]:
            toks = [t for t, _, _ in tokens(normalize(name))]
            while len(toks) > 1 and (toks[0].isdigit() or toks[0] in ("й", "я", "е")):
                toks = toks[1:]          # «5-й Предпортовый» → «Предпортовый»
            return tuple(stem(t) for t in toks)

        for a in self.reg.addresses:
            st = street_seq(a.street)
            self.streets.setdefault(st, []).append(("our", a))
        for n in self.reg.neighbors:
            for raw in n.get("addresses", []):
                pa = parse_address(raw)
                if pa:
                    st = street_seq(pa.street)
                    self.streets.setdefault(st, []).append(("neighbor", pa, n["key"], n.get("zhk", "") or ""))
        # для кодов без улицы («13к1 ЖК Прогресс»): ЖК → улицы его наших домов
        self.zhk_streets: dict[str, dict] = {}
        for sseq, entries in self.streets.items():
            for e in entries:
                if e[0] == "our" and e[1].zhk:
                    self.zhk_streets.setdefault(e[1].zhk, {}).setdefault((sseq, e[1].street_type, e[1].ordinal), []).append(e)

    # ---------------------------------------------------------------- основной метод
    def match(self, text: str, title: str = "", source_category: str = "", source_region: str = "",
              context_zhks: Iterable[str] = ()) -> Match:
        """context_zhks — ЖК из родительского поста: коды без улицы в комментарии («в 13к2 тоже…») сверяются с ними."""
        raw = normalize(f"{title}\n{text}" if title else text)
        low = raw.lower()
        toks = tokens(raw)
        st = [stem(t) for t, _, _ in toks]
        lem = [lemma(t) for t, _, _ in toks]
        m = Match()

        # 0) токены, занятые названиями НАШИХ улиц: «Авиаторов Балтики» — улица, а не ЖК «Авиатор»
        street_idx: set[int] = set()
        for sseq in self.streets:
            n = len(sseq)
            for i in range(len(st) - n + 1):
                if tuple(st[i:i + n]) == sseq and n >= 2:
                    street_idx.update(range(i, i + n))

        # 1) алиасы
        for i, s in enumerate(st):
            for a in self.aliases.get(s, ()):
                n = len(a.stems)
                if tuple(st[i:i + n]) != a.stems:
                    continue
                if a.surface_only and tuple(t for t, _, _ in toks[i:i + n]) != a.surface:
                    continue
                start, end = toks[i][1], toks[i + n - 1][2]
                if a.ambiguous and street_idx & set(range(i, i + n)) and (i == 0 or toks[i - 1][0] not in PREFIX_TOKENS):
                    continue
                conf, why = self._alias_conf(a, i, n, toks, lem, low, start, end)
                if conf <= 0:
                    continue
                m.hits.append(Hit(a.kind, a.key, conf, f"{raw[start:end]} ({why})", a.company, a.zhk, start, end))

        # 2) хэштеги (#жквиктория, #лампо, #цветарадуги)
        for h in hashtags(raw):
            for comp, a in self.compacts.items():
                if len(comp) < 4 or self._other_org(a, low):
                    continue
                if h == comp or h in (f"жк{comp}", f"ук{comp}") or (h.startswith(comp) and len(h) - len(comp) <= 8 and not a.ambiguous):
                    conf = 0.8 if (not a.ambiguous or h.startswith(("жк", "ук"))) else 0.35
                    m.hits.append(Hit(a.kind, a.key, conf, f"#{h}", a.company, a.zhk))

        # 3) нечёткие совпадения многословных названий (опечатки)
        found = {(h.kind, h.key) for h in m.hits}
        if len(low) < 12000:
            for a in self.multi:
                if (a.kind, a.key) in found or self._other_org(a, low):
                    continue
                score = fuzz.partial_ratio(a.text.lower(), low)
                if score >= 90:
                    conf = 0.55 if not a.ambiguous else 0.35
                    m.hits.append(Hit(a.kind, a.key, conf, f"≈{a.text} ({score:.0f}%)", a.company, a.zhk))
                    found.add((a.kind, a.key))

        # 4) адреса и улицы; коды корпусов без улицы рядом с ЖК; объяснение ЖК чужим зданием (дизайн §2–§3)
        used = self._match_streets(raw, low, toks, st, m)
        self._match_codes(raw, m, used, set(context_zhks or ()))
        self._explain(raw, m)

        # 5) кластеры, лексиконы, адресаты
        for ck, (kws, streets) in self.cluster_kw.items():
            if any(self._seq_in(st, k) for k in kws) or any(self._seq_in(st, s) for s in streets):
                m.clusters.append(ck)
        m.housing = sum(1 for l in lem if l in HOUSING) + len(HOUSING_RE.findall(low))
        neg = sum(1 for l in lem if l in NEGATIVE) + sum(2 for p in NEG_PHRASES if p in low)
        neg += low.count("!!") + (1 if re.search(r"[А-ЯЁ]{6,}", raw) else 0)
        m.negativity = min(1.0, neg * 0.2)
        m.positivity = min(1.0, sum(1 for l in lem if l in POSITIVE) * 0.3)
        m.addressees = [k for k, words in ADDRESSEES.items() if any(w in low for w in words)]
        if "gov_any" in m.addressees and ({"gov_spb", "gov_lo"} & set(m.addressees)):
            m.addressees.remove("gov_any")
        m.cta = any(p in low for p in CTA_PHRASES) or bool(CTA_PISHITE.search(low))
        m.hazard = [h for h in HAZARD if h in low]

        # дедупликация: одно и то же название, встреченное 5 раз, — одно совпадение с лучшей уверенностью
        best: dict[tuple, Hit] = {}
        for h in m.hits:
            k = (h.kind, h.key)
            if k not in best or h.conf > best[k].conf:
                best[k] = h
        m.hits = list(best.values())

        # 5б) без привязки к нашим регионам название-«тёзка» не считается: «ООО Коммуналсервис» в Удомле — не наш сосед
        regional = ((source_region or "").upper() in ("SPB", "LO") or bool(m.clusters) or bool(REGION_RE.search(low))
                    or any(h.kind in ("address", "street") for h in m.hits))
        if not regional:
            for h in m.hits:
                if (h.kind == "neighbor" and not h.evidence.startswith("адрес")) or (
                        h.kind == "company" and h.key in self.generic_companies):
                    h.conf = min(h.conf, 0.3)
                    h.evidence += " · нет привязки к СПб/ЛО"

        # уровень доказательства до объяснения (для наследования вердикта «та же история», дизайн §5.3)
        raw_hits = m.hits + m.explained_hits
        lvl = 1 if (m.clusters or any(h.kind in ("street", "neighbor") for h in raw_hits)) else 0
        if any(h.kind == "zhk" and h.conf >= 0.3 for h in raw_hits) or any(b.cls == "partial" for b in m.buildings):
            lvl = 2
        if any(b.cls == "ours" for b in m.buildings) or any(h.kind in ("company", "group") and h.conf >= 0.45 for h in raw_hits):
            lvl = 3
        m.raw_level = lvl
        m.partial = any(b.cls == "partial" for b in m.buildings)
        m.foreign = [{"label": b.label, "street": b.street, "house": f"{b.num}{b.litera}", "part": f"{b.part}{b.pv}",
                      "zhk": b.zhk, "known": b.known, "ours_here": b.ours_here[:6],
                      "explains": any(h.explained_by and h.zhk == b.zhk for h in m.explained_hits)}
                     for b in m.buildings if b.cls == "foreign" and b.zhk][:6]

        # 6) решение: кандидат / срочность
        ours = m.ours
        m.neighbor = any(h.kind == "neighbor" and h.conf >= 0.5 for h in m.hits)
        m.score = max([h.conf for h in ours], default=0.0)
        official = source_category in ("official", "governor")
        geo_complaint = bool(m.clusters) and m.housing >= 1 and (m.negativity > 0 or m.addressees)
        reasons = []
        if ours:
            reasons.append("наш объект")
        if m.neighbor:
            reasons.append("сосед по кварталу")
        if geo_complaint:
            reasons.append("жалоба в нашем кластере")
        if official and (m.clusters or (m.housing >= 2 and m.negativity > 0)):
            reasons.append("на площадке власти")
        m.candidate = bool(reasons)
        strong_neg = m.negativity >= 0.4 or bool(m.hazard) or m.cta
        gov = bool({"gov_spb", "gov_lo", "gov_any", "prosecutor", "gzhi_spb", "gzhn_lo", "sk", "rpn", "license"}
                   & set(m.addressees)) or official
        if ours and m.score >= 0.75 and (strong_neg or gov):
            m.urgency = 1
        elif (m.neighbor or geo_complaint) and gov and m.negativity > 0:
            m.urgency = 1
        m.reason = ", ".join(reasons)
        return m

    # ---------------------------------------------------------------- детали
    def _alias_conf(self, a: _Alias, i: int, n: int, toks, lem, low: str, start: int, end: int) -> tuple[float, str]:
        prev = toks[i - 1][0] if i > 0 else ""
        quoted = start > 0 and low[start - 1] in "\"'"
        prefixed = prev in PREFIX_TOKENS or (i > 1 and f"{toks[i-2][0]} {prev}" in ("жилой комплекс", "управляющая компания", "управляющей компании", "жилом комплексе"))
        if self._other_org(a, low):
            return 0.0, "другая организация"
        if a.negative and any(neg in low for neg in a.negative) and not prefixed:
            return 0.0, "негативный контекст"
        nxt = toks[i + n][0] if i + n < len(toks) else ""
        if nxt and re.search(r"[?!.;:\n)»\"]", low[end: toks[i + n][1]]):
            nxt = ""      # «…в "Цветах радуги"? 3. Нет света…» — после вопроса идёт номер пункта, а не дом
        if a.ambiguous and not prefixed and (nxt.isdigit() or nxt in STREET_TYPES or prev in STREET_TYPES):
            return 0.0, "это улица, а не название"
        base = 0.9 if n >= 2 else 0.8
        if a.kind == "neighbor":
            return (0.85 if (n >= 2 or a.surface_only) else 0.6), "сосед"
        if not a.ambiguous:
            return base, "точное"
        if a.prefix_required:   # «на 5 этаже» ≠ УК «Этажи»: нужен «УК …»/«ЖК …» или кавычки в жилищном контексте
            if prefixed:
                return 0.85, "с префиксом"
            if quoted and set(lem[max(0, i - 12): i + n + 12]) & self.ctx_lemmas:
                return 0.6, "в кавычках + жилищный контекст"
            return 0.0, ""
        if prefixed:
            return 0.85, "с префиксом ЖК/УК"
        window = set(lem[max(0, i - 12): i + n + 12])
        if window & self.ctx_lemmas:
            return (0.65 if quoted else 0.6), "жилищный контекст"
        if quoted and len(set(lem) & self.ctx_lemmas) >= 2:
            return 0.5, "в кавычках + контекст в тексте"
        return 0.15, "многозначно без контекста"

    @staticmethod
    def _other_org(a: _Alias, low: str) -> bool:
        return any(h in low for h in a.hard_negative)

    @staticmethod
    def _seq_in(st: list[str], seq: tuple[str, ...]) -> bool:
        if not seq:
            return False
        n = len(seq)
        return any(tuple(st[i:i + n]) == seq for i in range(len(st) - n + 1))

    def _match_streets(self, raw: str, low: str, toks, st, m: Match) -> list[tuple[int, int]]:
        """Улицы справочника и здания на них (дизайн §2): номер после улицы и перед ней, перечни, корпус в скобках,
        идентичность улицы (тип, ординал, род прилагательного), классы наше/частично/чужое/другой дом."""
        used: list[tuple[int, int]] = []
        for sseq, entries in self.streets.items():
            n = len(sseq)
            for i in range(len(st) - n + 1):
                if tuple(st[i:i + n]) != sseq:
                    continue
                s_start, s_end = toks[i][1], toks[i + n - 1][2]
                around = low[max(0, s_start - 14): s_end + 14]
                tm = STREET_TYPE_RE.search(around)
                typ = STREET_TYPES.get(tm.group(1).lower()) if tm else None
                pre = raw[max(0, s_start - 80): s_start]
                om = bl.STREET_ORD_RE.search(pre)
                ordinal = om.group(1) if om else ""
                pre_b = pre[:om.start()] if om else pre
                blds: list[bl.Building] = []
                am = bl.AFTER_RE.match(raw[s_end: s_end + 70])
                if am:
                    blds = bl.from_match(am, s_end, raw)
                    more, pos = bl.extend_list(raw, s_end + am.end(), blds[0])
                    blds += more
                    bl.paren_korpus(raw, pos, blds[-1])
                else:
                    bm = bl.BEFORE_RE.search(pre_b)
                    if bm and bm.group(0).strip():
                        blds = bl.from_match(bm, s_start - len(pre), raw)
                        if blds:
                            after = re.match(r"^\w*\.?\s*(?:" + bl.ST_TYPES_TEXT + r")?", raw[s_end: s_end + 30], re.I)
                            bl.paren_korpus(raw, s_end + (after.end() if after else 0), blds[-1])
                if not blds:
                    self._street_no_house(raw, s_start, s_end, typ, entries, m)
                    continue
                prim, sec = self._identities(entries, typ, ordinal, [t for t, _, _ in toks[i:i + n]])
                for b in blds:
                    bl.flags(raw, b)
                    self._building_hit(raw, b, prim, sec, s_start, s_end, m)
                    used.append((min(b.start, s_start), max(b.end, s_end)))
        return used

    @staticmethod
    def _identities(entries, typ, ordinal, name_tokens):
        """Кандидаты-улицы под одним стемом, отфильтрованные признаками текста (§2.2). Возвращает (основные, с
        несовпавшим типом). Признак, который отсекает всех, игнорируется; ординал из текста — жёсткий фильтр."""
        idents: dict[tuple, dict] = {}
        for e in entries:
            a = e[1]
            d = idents.setdefault((a.street_type, a.ordinal), {"street": a.street, "ours": [], "known": []})
            if e[0] == "our":
                d["ours"].append(a)
            else:
                d["known"].append((a, e[2], e[3]))
        keys = list(idents)
        if ordinal:
            keys = [k for k in keys if k[1] == ordinal] or keys
        sec: list = []
        if typ:
            same = [k for k in keys if k[0] == typ]
            sec = [k for k in keys if k[0] != typ]
            keys = same
        else:
            gens = set().union(*(adj_genders(t) for t in name_tokens)) if name_tokens else set()
            allowed = set().union(*(bl.GENDER_TYPES.get(g, set()) for g in gens)) if gens else set()
            if allowed:
                keys = [k for k in keys if k[0] in allowed] or keys
        return [idents[k] for k in keys], [idents[k] for k in sec]

    def _building_hit(self, raw: str, b: bl.Building, prim: list, sec: list, s_start: int, s_end: int, m: Match) -> None:
        import copy

        best = None          # (rank, conf, Building, identity)
        for group, penalty in ((prim, 0.0), (sec, 0.3)):
            for d in group:
                bb = copy.copy(b)
                cls = bl.classify(bb, d["ours"], d["known"])
                bb.street, bb.cls = d["street"], cls
                bb.zhks = {a.zhk for a in d["ours"] if a.zhk}
                if cls in ("ours", "partial"):
                    conf = (0.95 if cls == "ours" else 0.85) - penalty
                    if best is None or best[0] < 2 or conf > best[1]:
                        best = (2, conf, bb, d)
                elif penalty == 0.0 and (best is None or best[0] < 2):
                    rank = 1 if cls == "foreign" else 0
                    if best is None or rank > best[0]:
                        best = (rank, 0.25, bb, d)
        if best is None:
            return
        rank, conf, bb, d = best
        if rank == 1 and sum(1 for x in prim if any(a.num == b.num for a in x["ours"]) or
                             any(k[0].num == b.num for k in x["known"])) > 1:
            bb.cls, rank = "other", 0                   # номер есть у нескольких улиц-кандидатов — «чужого» нет
        b.__dict__.update(bb.__dict__)
        m.buildings.append(b)
        if b.cls in ("ours", "partial"):
            same = [x for x in d["ours"] if x.num == b.num] or d["ours"]
            a = next((x for x in same if x.litera == b.litera and (x.korpus or "") == (b.pv or "")), same[0])
            why = "" if b.cls == "ours" else " (корпус/часть не указаны или записаны иначе)"
            m.hits.append(Hit("address", a.key, round(conf, 2), f"адрес: {b.label}{why}", a.company, a.zhk, s_start, s_end))
        elif b.cls == "foreign":
            if b.known:
                m.hits.append(Hit("neighbor", b.known, 0.95, f"адрес соседа: {b.label}"))
            else:
                m.hits.append(Hit("street", compact(b.street), 0.25,
                                  f"улица: {b.label} — не наш корпус (наши: {', '.join(b.ours_here[:6])})", "", "", s_start, s_end))
        else:
            a = d["ours"][0] if d["ours"] else None
            if a:
                m.hits.append(Hit("street", compact(a.street), 0.25, f"улица: {b.label}", a.company, a.zhk, s_start, s_end))
            elif d["known"]:
                m.hits.append(Hit("street", d["known"][0][1], 0.3, f"адрес соседа: {b.label}"))

    def _street_no_house(self, raw, s_start, s_end, typ, entries, m: Match) -> None:
        """Улица без номера дома — как раньше: 0.35 (наша улица) / 0.3 (улица соседа)."""
        best = None
        for e in entries:
            addr = e[1]
            conf = max(0.0, 0.35 - (0.3 if (typ and addr.street_type and typ != addr.street_type) else 0.0))
            if best is None or conf > best[0]:
                best = (conf, e)
        if not best:
            return
        conf, e = best
        label = raw[s_start:s_end]
        if e[0] == "neighbor":
            m.hits.append(Hit("street", e[2], 0.3, f"адрес соседа: {label}"))
        else:
            a = e[1]
            m.hits.append(Hit("street", compact(a.street), conf, f"улица: {label}", a.company, a.zhk, s_start, s_end))

    def _match_codes(self, raw: str, m: Match, used: list, ctx_zhks: set) -> None:
        """Коды корпусов без улицы («13к1 ЖК Прогресс», «в 13к2 тоже» под постом про ЖК): сверка с домами ЖК (§2.1)."""
        zpos = [(h.zhk or h.key, h.start, h.end) for h in m.hits if h.kind == "zhk" and h.conf >= 0.3]
        if not (zpos or ctx_zhks):
            return
        for cm in bl.CODE_RE.finditer(raw):
            if any(a <= cm.start() < e for a, e in used):
                continue
            near = {z for z, a, e in zpos if (a or e) and (abs(cm.start() - e) <= 70 or abs(a - cm.end()) <= 70)}
            for z in (near or ctx_zhks):
                cands = [(k, es) for k, es in self.zhk_streets.get(z, {}).items() if any(e[1].num == cm.group(1) for e in es)]
                if len(cands) != 1:
                    continue                                   # номера нет среди домов ЖК или он на нескольких улицах
                (sseq, typ, ordn), _ = cands[0]
                prim = [d for d in self._identities(self.streets[sseq], typ, ordn, [])[0]]
                b = bl.Building(cm.group(1), part="к", pv=cm.group(2), start=cm.start(), end=cm.end())
                bl.flags(raw, b)
                self._building_hit(raw, b, prim, [], cm.start(), cm.end(), m)
                used.append((cm.start(), cm.end()))
                break

    def _explain(self, raw: str, m: Match) -> None:
        zh = [(h.zhk or h.key, h.start, h.end) for h in m.hits if h.kind == "zhk" and h.conf >= 0.3]
        if not zh or not any(b.cls == "foreign" for b in m.buildings):
            return
        if any(b.cls in ("ours", "partial") for b in m.buildings) or \
                any(h.kind in ("company", "group", "address") and h.conf >= 0.45 for h in m.hits):
            return                                         # наш дом, частичный адрес, наша УК/ГК не объясняются никогда
        from .rules import topic_hits                      # rules импортирует matcher — ленивый импорт

        keys, labels, blocked = bl.explain(raw, zh, m.buildings, topic_hits)
        m.blocked = blocked
        if not keys:
            return
        keep = []
        for h in m.hits:
            if h.kind == "zhk" and (h.zhk or h.key) in keys and h.conf >= 0.3:
                h.explained_by = labels[h.zhk or h.key]
                m.explained_hits.append(h)
            else:
                keep.append(h)
        m.hits = keep

    # ---------------------------------------------------------------- подсказка для ИИ/людей
    def hint(self, m: Match) -> str:
        parts = []
        for h in sorted(m.hits, key=lambda h: -h.conf)[:8]:
            if h.kind == "street" and str(h.evidence).startswith("улица:"):
                # 23.10: «street:… → ЖК Дом-Мегалит» модель читала как доказательство «наш ЖК». Улица — не дом.
                ours = sorted({f"{a.num}{a.litera}" + (f"к{a.korpus}" if a.korpus else "")
                               for a in self.reg.addresses if compact(a.street) == h.key})[:6]
                state = "номер дома НЕ назван" if h.conf >= 0.3 else "назван ДРУГОЙ дом"
                parts.append(f"улица из справочника: {h.evidence.replace('улица:', '').strip()} — {state}; наших домов на ней: "
                             f"{', '.join(ours) or '—'}, остальные дома чужие → это НЕ наш объект: relation=cluster, "
                             f"наш ЖК/УК не называй [{h.conf:.2f}]")
                continue
            if h.kind == "zhk" and self.reg.zhk_shared(h.zhk or h.key) and not any(
                    x.kind in ("company", "group", "address") and x.conf >= 0.45 for x in m.hits):
                # 03.11: «→ ЖК Солнечный Город; ООО «Цивилизация»» модель читала как «наша УК». В общем ЖК это не так.
                z = h.zhk or h.key
                parts.append(f"ЖК {self.reg.zhk_name(z)} ({h.evidence}) — в этом ЖК есть дома ДРУГИХ УК; наш дом и наша УК "
                             f"в тексте не названы → это может быть чужой дом: relation=cluster, не называй нашу УК [{h.conf:.2f}]")
                continue
            who = []
            if h.zhk:
                who.append("ЖК " + self.reg.zhk_name(h.zhk))
            if h.company:
                who.append(", ".join(self.reg.company_name(c) for c in h.company.split(",") if c))
            if h.kind == "neighbor":
                who.append(next((n["name"] for n in self.reg.neighbors if n["key"] == h.key), h.key))
            parts.append(f"{h.kind}:{h.evidence} → {'; '.join(who) or h.key} [{h.conf:.2f}]")
        for f in m.foreign:
            zname = self.reg.zhk_name(f["zhk"])
            num = re.match(r"\d+", f["house"]).group(0) if re.match(r"\d+", f["house"]) else f["house"]
            ours = ", ".join(x.split(" ")[-1] for x in f["ours_here"]) or "—"
            tail = "" if (f["known"] or self.reg.zhk_complete(f["zhk"])) else "; дом не из справочника, справочник ЖК неполный"
            parts.insert(0, f"⚠ ЖК {zname}: в тексте дом {f['label']} — НЕ наш (наши в доме {num}: {ours}){tail} → relation=neighbor")
        if m.clusters:
            parts.append("кластер: " + ", ".join(m.clusters))
        if m.addressees:
            parts.append("адресат: " + ", ".join(ADDRESSEE_TITLES.get(a, a) for a in m.addressees))
        if m.hazard:
            parts.append("угроза: " + ", ".join(m.hazard[:4]))
        if m.cta:
            parts.append("призыв к действию")
        parts.append(f"негатив≈{m.negativity:.1f}, ЖКХ-слов={m.housing}")
        return " | ".join(parts)
