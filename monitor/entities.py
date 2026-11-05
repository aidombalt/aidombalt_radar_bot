"""Справочник объектов: УК группы, ЖК, адреса, соседи, гео-кластеры.

Источник правды — config/entities.yaml (редактируется в GUI). Импорт из «Информация о ГК.docx»
делает черновик (entities.imported.yaml) — его можно сравнить и принять.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import CONFIG_DIR
from .textnorm import compact, normalize

STREET_TYPES = {
    "улица": "ул", "ул": "ул", "проспект": "пр", "пр-т": "пр", "пр": "пр", "просп": "пр",
    "бульвар": "б-р", "б-р": "б-р", "бул": "б-р", "шоссе": "ш", "ш": "ш", "набережная": "наб", "наб": "наб",
    "проезд": "пр-д", "пр-д": "пр-д", "дорога": "дор", "дор": "дор", "пр-кт": "пр", "бульв": "б-р", "переулок": "пер", "пер": "пер", "аллея": "ал",
    "площадь": "пл", "пл": "пл", "линия": "лин",
}
_ST_ALT = "|".join(sorted((re.escape(k) for k in STREET_TYPES), key=len, reverse=True))
STREET_TYPE_RE = re.compile(rf"(?<![а-яa-z])({_ST_ALT})\.?(?![а-яa-z])", re.I)
HOUSE_RE = re.compile(
    r"(?:д(?:ом)?\.?\s*)?(\d{1,3})\s*([а-е])?(?:\s*(?:,\s*)?(?:к(?:орп(?:ус)?)?\.?|/|стр(?:оение)?\.?|-)\s*(\d{1,2}))?",
    re.I,
)


@dataclass
class Address:
    raw: str
    street: str               # «Авиаторов Балтики»
    street_type: str          # «пр»
    house: str                # «9»
    korpus: str = ""          # «1»
    company: str = ""         # ключ УК
    zhk: str = ""             # ключ ЖК
    locality: str = "Санкт-Петербург"
    district: str = ""
    part: str = ""            # вид части номера: «к» (корпус), «стр» (строение), «/» (дробь), «-»; значение — в korpus
    ordinal: str = ""         # «5» для «5-й Предпортовый»

    @property
    def key(self) -> str:
        return f"{compact(self.street)}|{self.house}|{self.korpus}"

    @property
    def num(self) -> str:
        """Основной номер без литеры: «3А» → «3»."""
        return re.match(r"\d+", self.house).group(0) if re.match(r"\d+", self.house) else self.house

    @property
    def litera(self) -> str:
        return self.house[len(self.num):].upper()

    @property
    def label(self) -> str:
        sep = {"к": "к", "стр": " стр. ", "/": "/", "-": "-"}.get(self.part, "к")
        return f"{self.street} {self.house}{(sep + self.korpus) if self.korpus else ''}"


@dataclass
class Registry:
    companies: dict[str, dict] = field(default_factory=dict)
    zhks: dict[str, dict] = field(default_factory=dict)
    addresses: list[Address] = field(default_factory=list)
    neighbors: list[dict] = field(default_factory=list)
    clusters: list[dict] = field(default_factory=list)
    context: dict[str, list[str]] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    learned: dict = field(default_factory=dict)

    def zhk_shared(self, key: str) -> bool:
        """В ЖК есть дома других УК (shared: true в справочнике): название ЖК без нашего дома или нашей УК — не
        доказательство «наш объект». 03.11: «ЖК Солнечный город» — там с нами соседствуют несколько УК."""
        return bool((self.zhks.get(key) or {}).get("shared"))

    def zhk_complete(self, key: str) -> bool:
        """Справочник ЖК полный: сверен (verified) и не помечен complete: false. Без отметки — неполный."""
        z = self.zhks.get(key) or {}
        return bool(z.get("verified")) and z.get("complete") is not False

    def ours_in_house(self, street: str, num: str) -> list["Address"]:
        return [a for a in self.addresses if compact(a.street) == compact(street) and a.num == num]

    def company_name(self, key: str) -> str:
        return self.companies.get(key, {}).get("name", key)

    def zhk_name(self, key: str) -> str:
        return self.zhks.get(key, {}).get("name", key)

    def catalog_text(self, max_len: int = 6000) -> str:
        """Короткая сводка для промпта LLM."""
        lines = [f"Группа: {self.raw.get('group', {}).get('name', 'ГК')}. Наши УК и объекты:"]
        for ck, c in self.companies.items():
            zs = sorted({a.zhk for a in self.addresses if a.company == ck and a.zhk})
            locs = sorted({a.locality for a in self.addresses if a.company == ck})
            znames = ", ".join(self.zhk_name(z) for z in zs) or "отдельные дома"
            hidden = " [СВЯЗЬ С ГК НЕ АФИШИРУЕТСЯ: отвечать только от имени этой УК, ГК и другие УК не упоминать]" \
                if c.get("public") is False else ""
            lines.append(f"- {c['name']} (алиасы: {', '.join(c.get('aliases', [])[:4])}): ЖК {znames}; {', '.join(locs)}{hidden}")
        if self.neighbors:
            lines.append("Соседи/конкуренты (НЕ наши, но риск переноса негатива на наш квартал):")
            for n in self.neighbors:
                lines.append(f"- {n['name']} — {n.get('note', '')} ({', '.join(n.get('addresses', [])[:4])})")
        return "\n".join(lines)[:max_len]


def parse_address(raw: str) -> Address | None:
    s = normalize(raw).strip().strip(";,. ")
    m = STREET_TYPE_RE.search(s)
    st_type = STREET_TYPES.get(m.group(1).lower(), "ул") if m else "ул"
    body = (s[: m.start()] + " " + s[m.end():]) if m else s
    hm = None
    for cand in HOUSE_RE.finditer(body):
        if cand.group(1) and not re.match(r"^\s*-?\s*(?:й|я|е|ый|ая|ой)\b", body[cand.end(1):]):
            hm = cand
            break
    if not hm:
        return None
    street = body[: hm.start()]
    street = re.sub(r"\b(д|дом)\.?\s*$", "", street.strip(" ,."), flags=re.I).strip(" ,.")
    if not street:
        return None
    house = hm.group(1) + (hm.group(2) or "").upper()
    sep = body[hm.end(2) if hm.group(2) else hm.end(1): hm.start(3)] if hm.group(3) else ""
    part = ("стр" if "стр" in sep.lower() else "/" if "/" in sep else "-" if "-" in sep else "к") if hm.group(3) else ""
    om = re.match(r"\s*(\d{1,2})\s*-?\s*[а-я]{0,3}\s+", street)
    return Address(raw=raw.strip(), street=street, street_type=st_type, house=house, korpus=hm.group(3) or "",
                   part=part, ordinal=om.group(1) if om else "")


# ---------------------------------------------------------------- docx-импорт (черновик)
_COMPANY_RE = re.compile(r"^\s*(?:\d+\.\s*)?ООО\s*[«\"]?([^»\"(]+)[»\"]?", re.I)
_ZHK_RE = re.compile(r"(?:ЖК\s*)?[«\"]([^»\"]{2,60})[»\"]")
_COMPACT_LIST_RE = re.compile(
    rf"^((?:(?:{_ST_ALT})\.?\s+)?(?:\d-й\s+)?[А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁ][а-яё]+)?(?:\s+(?:{_ST_ALT})\.?)?)"
    r"[\s,]+(?:д\.\s*)?(\d+(?:\s*к\d+)?(?:\s*,\s*\d+(?:\s*к\d+)?)+)\s*$", re.I)


def _split_addresses(chunk: str) -> list[str]:
    chunk = chunk.strip()
    if not chunk:
        return []
    m = _COMPACT_LIST_RE.match(chunk)
    if m:  # «Буденного 19к1, 19к2, 21к1»
        street = m.group(1)
        return [f"{street} {n.strip()}" for n in m.group(2).split(",")]
    parts = re.split(rf",\s*(?=(?:{_ST_ALT})\.?\s|[А-ЯЁ][а-яё]+\s+(?:{_ST_ALT})\b)", chunk, flags=re.I)
    return [p.strip() for p in parts if re.search(r"\d", p)]


def import_docx(path: str | Path) -> dict:
    import docx  # python-docx

    d = docx.Document(str(path))
    paras = [normalize(p.text).strip() for p in d.paragraphs if p.text.strip()]
    companies: list[dict] = []
    cur: dict | None = None
    cur_zhk = ""
    for p in paras:
        cm = _COMPANY_RE.match(p)
        if cm and len(p) < 120:
            cur = {"name": f"ООО «{cm.group(1).strip()}»", "objects": []}
            companies.append(cur)
            cur_zhk = ""
            rest = p[cm.end():]
            if "ЖК" in rest:
                zm = _ZHK_RE.search(rest)
                cur_zhk = zm.group(1).strip() if zm else cur_zhk
            continue
        if cur is None:
            continue
        for chunk in re.split(r";|\.\s+(?=ЖК)|(?<=\d)\s+(?=ЖК\s*[«\"])", p):
            chunk = chunk.strip()
            if not chunk:
                continue
            zm = _ZHK_RE.search(chunk)
            if zm and (chunk.startswith("ЖК") or zm.start() < 4):
                cur_zhk = zm.group(1).strip()
                chunk = re.split(r"\s-{1,3}\s|\s—\s", chunk, maxsplit=1)[1] if re.search(r"\s-{1,3}\s|\s—\s", chunk) else ""
            elif re.match(r"^ЖК\s", chunk):
                cur_zhk = chunk[3:].strip(" «»\"")
                continue
            chunk = re.sub(r"\(Прежние наименования:.*?\)", "", chunk)
            for a in _split_addresses(chunk):
                pa = parse_address(a)
                if pa:
                    cur["objects"].append({"zhk": cur_zhk, "address": a})
    return {"companies": companies}


# ---------------------------------------------------------------- загрузка
def load_registry(path: Path | None = None) -> Registry:
    path = path or CONFIG_DIR / "entities.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    reg = Registry(raw=raw or {})
    for c in raw.get("companies", []):
        reg.companies[c["key"]] = c
    for z in raw.get("zhk", []):
        reg.zhks[z["key"]] = z
    for c in raw.get("companies", []):
        for obj in c.get("objects", []):
            for a in obj.get("addresses", []):
                pa = parse_address(a)
                if not pa:
                    continue
                pa.company = c["key"]
                pa.zhk = obj.get("zhk", "") or ""
                pa.locality = obj.get("locality", c.get("locality", "Санкт-Петербург"))
                pa.district = obj.get("district", "")
                reg.addresses.append(pa)
    reg.neighbors = list(raw.get("neighbors", []))
    reg.clusters = raw.get("clusters", [])
    reg.context = raw.get("context", {})
    _merge_learned(reg, path.parent / LEARNED_FILE)
    return reg


# ---------------------------------------------------------------- полнота справочника по ЖК (галочка в GUI)
_VER_RE = re.compile(r',\s*verified:\s*"[^"]*"(?:,\s*verified_source:\s*"[^"]*")?')


def set_zhk_verified(key: str, on: bool, source: str = "", date: str = "", path: Path | None = None) -> bool:
    """Ставит/снимает отметку «список наших домов ЖК полный» прямо в строке ЖК, не трогая комментарии файла.
    Возвращает False, если строка ЖК не найдена (ЖК записан не одной строкой — правьте YAML вручную)."""
    path = path or CONFIG_DIR / "entities.yaml"
    lines = path.read_text(encoding="utf-8").split("\n")
    rx = re.compile(r"^(\s*-\s*\{\s*key:\s*" + re.escape(key) + r"\s*,.*)\}\s*$")
    for i, line in enumerate(lines):
        m = rx.match(line)
        if not m:
            continue
        body = _VER_RE.sub("", m.group(1)).rstrip()
        if on:
            src = source.replace('"', "'")
            body += f', verified: "{date}", verified_source: "{src}"'
        lines[i] = body + "}"
        path.write_text("\n".join(lines), encoding="utf-8")
        return True
    return False


_SHARED_RE = re.compile(r',\s*shared:\s*(?:true|false)(?:,\s*shared_note:\s*"[^"]*")?')


def set_zhk_shared(key: str, on: bool, note: str = "", path: Path | None = None) -> bool:
    """Ставит/снимает «в ЖК есть дома других УК» прямо в строке ЖК (как set_zhk_verified)."""
    path = path or CONFIG_DIR / "entities.yaml"
    lines = path.read_text(encoding="utf-8").split("\n")
    rx = re.compile(r"^(\s*-\s*\{\s*key:\s*" + re.escape(key) + r"\s*,.*)\}\s*$")
    for i, line in enumerate(lines):
        m = rx.match(line)
        if not m:
            continue
        body = _SHARED_RE.sub("", m.group(1)).rstrip()
        if on:
            body += ', shared: true' + (f', shared_note: "{note.replace(chr(34), chr(39))}"' if note else "")
        lines[i] = body + "}"
        path.write_text("\n".join(lines), encoding="utf-8")
        return True
    return False


# ---------------------------------------------------------------- решения оператора (дизайн §6)
LEARNED_FILE = "entities.learned.yaml"


def load_learned(path: Path | None = None) -> dict:
    path = path or CONFIG_DIR / LEARNED_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001 — испорченный файл не должен ронять запуск
        data = {}
    data = data or {}
    return {"foreign_buildings": list(data.get("foreign_buildings") or []), "our_buildings": list(data.get("our_buildings") or [])}


def save_learned(data: dict, path: Path | None = None) -> None:
    """Атомарная запись: временный файл + замена (обрыв посередине не портит файл)."""
    path = path or CONFIG_DIR / LEARNED_FILE
    tmp = path.with_suffix(".tmp")
    head = ("# Решения операторов из бота: «🏠 Наш дом» / «🚫 Не наш». Пользовательский файл: обновление архива его не\n"
            "# перезаписывает. Удалить запись можно здесь или в GUI → «Объекты».\n")
    tmp.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _merge_learned(reg: Registry, path: Path) -> None:
    data = load_learned(path)
    reg.learned = data
    for i, fb in enumerate(data["foreign_buildings"]):
        if fb.get("address"):
            reg.neighbors.append({"key": f"learned{i + 1}", "name": f"дом не наш ({fb['address']})", "aliases": [],
                                  "addresses": [fb["address"]], "zhk": fb.get("zhk", ""), "learned": True,
                                  "note": fb.get("note") or "отмечено оператором"})
    for ob in data["our_buildings"]:
        pa = parse_address(ob.get("address") or "")
        if not pa or ob.get("company") not in reg.companies:
            continue
        pa.company, pa.zhk = ob["company"], ob.get("zhk", "") or ""
        same = [a for a in reg.addresses if a.zhk == pa.zhk and a.locality] or [a for a in reg.addresses if a.company == pa.company]
        pa.locality = same[0].locality if same else "Санкт-Петербург"
        if not any(a.key == pa.key for a in reg.addresses):
            reg.addresses.append(pa)


def registry_summary(reg: Registry) -> dict[str, Any]:
    return {
        "companies": len(reg.companies), "zhk": len(reg.zhks), "addresses": len(reg.addresses),
        "neighbors": len(reg.neighbors), "clusters": len(reg.clusters),
    }
