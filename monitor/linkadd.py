"""Добавление по ссылке. Вставили t.me/…, @имя, vk.com/… или сайт СМИ → Радар сам понимает, что это
(канал, паблик, страница человека, лента СМИ), показывает карточку — название, аудиторию, последний пост —
и добавляет как источник или ставит человека на контроль. Используется и в веб-интерфейсе, и в боте."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from .collectors import portable
from .settings import CONFIG_DIR
from .util import ago, from_ts, iso, jdump, jload, msk

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger("radar.linkadd")

TG_PRIVATE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)", re.I)
TG_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})(?:/\d+)?/?(?:[?#].*)?$", re.I)
AT_RE = re.compile(r"^@([A-Za-z0-9_]{4,})$")
VK_RE = re.compile(r"^(?:https?://)?(?:m\.|www\.)?vk\.(?:com|ru)/([A-Za-z0-9_.\-]+)/?(?:[?#].*)?$", re.I)
VK_WALL = re.compile(r"^wall(-?\d+)_\d+$")
URL_RE = re.compile(r"^https?://[^\s]+$", re.I)

LO_WORDS = re.compile(r"мурин|кудров|девяткин|всеволож|ленобл|ленинградск|сертолов|бугр|янин|\bло\b|\b47\b", re.I)
SPB_WORDS = re.compile(r"спб|петербург|питер|\b78\b|приморск|выборгск|невск|красногвард|фрунзенск|московск|"
                       r"калининск|кировск|адмиралтейск|петроградск|василеостров|колпин|пушкин|парнас", re.I)

# Человеческие подписи для интерфейса
CATEGORIES = {"public": "Паблик / чат района", "official": "Власть и надзор", "governor": "Губернатор",
              "media": "СМИ", "influencer": "Блогер / активист", "own": "Наша группа УК (жалобы в комментариях)"}
TIERS = {"hot": "каждые 5 мин", "warm": "каждые 15 мин", "cold": "раз в 3 часа"}
KINDS = {"vk_wall": "ВКонтакте", "tg_channel": "Telegram-канал", "rss": "Сайт (лента новостей)", "web": "Сайт",
         "vk_search": "Поиск ВК", "gnews": "Google News", "manual": "Пересланное в бот"}


@dataclass
class Found:
    platform: str                  # vk | tg | web
    handle: str
    link: str = ""
    kind: str = ""                 # vk_wall | tg_channel | rss | tg_person
    title: str = ""
    members: int = 0
    person: bool = False           # страница человека, а не сообщества
    uid: int | None = None         # ВК: id пользователя (>0) или сообщества (<0)
    last_at: str | None = None
    sample: str = ""
    region: str = ""
    category: str = "public"
    tier: str = "warm"
    comments: bool = False
    notes: list[str] = field(default_factory=list)
    error: str = ""
    exists: str | None = None      # ключ, если уже отслеживается

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """Одна строка для карточки: «Telegram-канал · 28 100 подписчиков · последний пост 24.09 13:21»."""
        what = ("страница человека во ВКонтакте" if self.person and self.platform == "vk" else
                "аккаунт Telegram" if self.kind == "tg_person" else KINDS.get(self.kind, self.kind))
        parts = [what]
        if self.members:
            parts.append(f"{self.members:,} подписчиков".replace(",", " "))
        if self.last_at:
            parts.append(f"последний пост {msk(self.last_at, '%d.%m %H:%M')}")
        return " · ".join(parts)


def human_error(msg: str | None) -> str:
    """«HTTPStatusError: Client error '403 Forbidden' for url … For more information…» → «доступ запрещён (403)»."""
    m = msg or ""
    for pat, text in ((r"\b403\b", "площадка не пускает (403)"), (r"\b404\b", "страница не найдена (404)"),
                      (r"\b5\d\d\b", "сбой на стороне площадки"), (r"ReadTimeout|TimeoutError|ConnectTimeout", "не ответила вовремя"),
                      (r"ConnectError|ReadError|RemoteProtocolError", "обрыв соединения"),
                      (r"VK 5\b|access_token", "ключ ВК недействителен"), (r"VK 29\b", "исчерпан суточный лимит ВК (сбросится ночью)"), (r"VK 30\b|private", "страница закрыта"),
                      (r"VK 15\b|Access denied", "нет доступа к записям"), (r"RSS не найден", "лента не найдена")):
        if re.search(pat, m):
            return text
    return re.sub(r"https?://\S+|For more information.*$", "", m).strip()[:120]


def similar_title(ctx: "Ctx", title: str) -> str | None:
    """Тот же паблик под другим адресом? SQLite lower() не знает кириллицу — сравниваем в Python, без эмодзи и знаков."""
    def norm(s: str) -> str:
        return " ".join(re.sub(r"[^\w]+", " ", (s or "").casefold().replace("ё", "е")).split())
    want = norm(title)
    if len(want) < 4:
        return None
    for r in ctx.db.q("SELECT title FROM sources WHERE candidate<>2 AND title IS NOT NULL"):
        if norm(r["title"]) == want:
            return r["title"]
    return None


def watched_by_link(ctx: "Ctx", f: "Found") -> str | None:
    for p in ctx.watch.people:
        if f.platform == "vk" and str(p.get("vk_id") or "").lower() == f.handle.lower():
            return p["name"]
        if f.platform == "tg" and str(p.get("tg") or "").lower() == f.handle.lower():
            return p["name"]
    return None


def parse(link: str) -> tuple[str, str] | str:
    """(платформа, адрес) или текст ошибки. Понимает ссылки, @имя, club123/id123, ссылки на посты."""
    s = (link or "").strip().strip("<>").rstrip(".,;")
    if not s:
        return "Вставьте ссылку"
    if TG_PRIVATE.search(s):
        return "Это приглашение в закрытый чат — Радар может читать только публичные каналы"
    if m := TG_RE.match(s) or AT_RE.match(s):
        return "tg", m.group(1)
    if m := VK_RE.match(s):
        h = m.group(1)
        if w := VK_WALL.match(h):   # ссылка на пост → стена его владельца
            oid = int(w.group(1))
            h = f"club{-oid}" if oid < 0 else f"id{oid}"
        if h.lower() in ("feed", "im", "search", "groups", "friends", "video", "music", "wall"):
            return "Это служебная страница ВК, а не паблик или человек"
        return "vk", h
    if re.fullmatch(r"(?:club|public|id)\d+", s, re.I):
        return "vk", s
    if URL_RE.match(s):
        return "web", s
    if re.fullmatch(r"[A-Za-z0-9_.]{4,}", s):
        return "Непонятно, где это: пришлите ссылку целиком (t.me/…, vk.com/…) или @имя для Telegram"
    return "Не похоже на ссылку. Примеры: t.me/kudrovo_life · @kudrovo_life · vk.com/murino_live · https://fontanka.ru"


def guess_region(*texts: str) -> str:
    t = " ".join(x or "" for x in texts)
    return "LO" if LO_WORDS.search(t) else "SPB" if SPB_WORDS.search(t) else ""


def guess_category(title: str, person: bool, platform: str) -> str:
    t = (title or "").lower()
    if person:
        return "influencer"
    if re.search(r"губернатор|беглов|дрозденко", t):
        return "governor"
    if re.search(r"администрац|правительств|комитет|жилнадзор|жилинспекц|\bгжи\b|прокуратур|мчс|госжил", t):
        return "official"
    if platform == "web" or re.search(r"новост|\bсми\b|тв\b|газет|радио|телеканал|медиа|фонтанка|бумага", t):
        return "media"
    return "public"


def existing_key(ctx: "Ctx", kind: str, handle: str) -> str | None:
    r = ctx.db.one("SELECT key FROM sources WHERE kind=? AND lower(handle)=lower(?) AND candidate<>2", (kind, handle))
    return r["key"] if r else None


# ---------------------------------------------------------------------------------------------- распознавание
async def preview(ctx: "Ctx", service, link: str) -> Found:
    p = parse(link)
    if isinstance(p, str):
        return Found("", "", link=link, error=p)
    platform, handle = p
    f = Found(platform, handle, link=link)
    try:
        if platform == "vk":
            await _vk(ctx, service, f)
        elif platform == "tg":
            await _tg(ctx, service, f)
        else:
            await _web(ctx, service, f)
    except Exception as e:  # noqa: BLE001
        log.info("preview %s: %s", link, e)
        f.error = f.error or f"Не удалось открыть: {type(e).__name__}: {str(e)[:150]}"
    if not f.error:
        f.region = f.region or guess_region(f.title, f.sample)
        f.category = guess_category(f.title, f.person, platform)
        f.tier = "hot" if f.category in ("governor", "official", "influencer") or f.region == "LO" else "warm"
        if f.kind in ("vk_wall", "tg_channel", "rss"):
            f.exists = existing_key(ctx, f.kind, f.handle)
    return f


async def _vk(ctx: "Ctx", service, f: Found) -> None:
    api = service.vk.api
    if not api.ready:
        f.error = "Не задан ключ ВК (VK_SERVICE_TOKEN) — проверить страницу нельзя"
        return
    h = f.handle
    if m := re.fullmatch(r"id(\d+)", h, re.I):
        typ, oid = "user", int(m.group(1))
    elif m := re.fullmatch(r"(?:club|public|event)(\d+)", h, re.I):
        typ, oid = "group", int(m.group(1))
    else:
        r = await api.call("utils.resolveScreenName", {"screen_name": h})
        if not r:
            f.error = f"Страница «{h}» во ВКонтакте не найдена"
            return
        typ, oid = r["type"], int(r["object_id"])
    f.kind = "vk_wall"
    if typ == "user":
        u = (await api.call("users.get", {"user_ids": oid, "fields": "followers_count,screen_name,city"}))[0]
        f.person, f.uid = True, oid
        f.title = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        f.members = u.get("followers_count", 0) or 0
        f.handle = u.get("screen_name") or f"id{oid}"
        f.region = guess_region((u.get("city") or {}).get("title", ""))
        if u.get("is_closed") and not u.get("can_access_closed"):
            f.notes.append("Профиль закрыт: стену не читаем, но комментарии этого человека в открытых пабликах "
                           "будут отмечаться.")
        owner = oid
    elif typ in ("group", "page", "event"):
        g = await api.call("groups.getById", {"group_id": oid, "fields": "members_count,description,city"})
        g = (g.get("groups") if isinstance(g, dict) else g)[0]
        f.uid, f.title = -oid, g.get("name", "")
        f.members = g.get("members_count", 0) or 0
        f.handle = g.get("screen_name") or f"club{oid}"
        f.comments = True
        f.region = guess_region(g.get("name", ""), (g.get("city") or {}).get("title", ""), g.get("description", "")[:300])
        if g.get("is_closed"):
            f.notes.append("Сообщество закрытое: без вступления посты не читаются.")
        owner = -oid
    else:
        f.error = "Это приложение или другой тип страницы ВК — добавить нельзя"
        return
    try:
        w = await api.call("wall.get", {"owner_id": owner, "count": 3})
        posts = [x for x in (w or {}).get("items", []) if not x.get("is_pinned")] or (w or {}).get("items", [])
        if posts:
            f.last_at = from_ts(posts[0].get("date", 0))
            f.sample = (posts[0].get("text") or "").strip()[:300]
    except Exception as e:  # noqa: BLE001
        if not f.notes:
            f.notes.append(f"Стена не читается ({str(e)[:80]}).")


async def _tg(ctx: "Ctx", service, f: Found) -> None:
    client = service.collectors["tg_channel"].client
    items, info = await portable.fetch_tg_channel(client, f.handle, 0, max_pages=1)
    if items or info.get("subscribers"):
        f.kind, f.title, f.members = "tg_channel", info.get("title") or f.handle, info.get("subscribers", 0)
        if items:
            last = max(items, key=lambda i: i["msg_id"])
            f.last_at, f.sample = last.get("published_at"), (last.get("text") or "")[:300]
        return
    # нет веб-ленты: это человек, группа-чат или канал с закрытым просмотром — смотрим обычную страницу t.me
    r = await client.get(f"https://t.me/{f.handle}")
    page = r.text or ""
    title, extra = _grab("tgme_page_title", page), _grab("tgme_page_extra", page).lower()
    if not title:
        f.error = f"Telegram не знает @{f.handle}: проверьте написание"
        return
    f.title = title
    if "subscriber" in extra or "подписчик" in extra:
        f.error = "У этого канала закрыт веб-просмотр — Радар его прочитать не сможет"
    elif "member" in extra or "участник" in extra or "online" in extra:
        f.error = "Это группа-чат, а не канал: Радар читает только каналы с открытым просмотром"
    else:
        f.kind, f.person = "tg_person", True
        f.notes.append("Личный аккаунт: сообщения человека прочитать нельзя, но Радар будет отмечать публикации, "
                       f"где упоминаются @{f.handle} или его имя.")


def _grab(cls: str, page: str) -> str:
    m = re.search(rf'class="{cls}[^"]*"[^>]*>(.*?)</div>', page, re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""


async def _web(ctx: "Ctx", service, f: Found) -> None:
    client = service.collectors["rss"].client
    f.kind = "rss"
    feed = await portable.discover_rss(client, f.handle)
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", f.handle).split("/")[0])
    if feed:
        r = await client.get(feed)
        entries = portable.parse_feed(r.content)
        m = re.search(r"<title>(.*?)</title>", r.text[:3000], re.S)
        f.title = html.unescape(m.group(1)).strip() if m else host
        if entries:
            f.last_at, f.sample = entries[0].get("published_at"), (entries[0].get("title") or "")[:300]
    else:
        f.title = host
        f.notes.append("RSS-ленты нет — статьи этого издания Радар будет брать из Google News (поиск по сайту).")


# ---------------------------------------------------------------------------------------------- действия
def add_source(ctx: "Ctx", f: Found, *, title: str | None = None, category: str | None = None, tier: str | None = None,
               region: str | None = None, comments: bool | None = None, by: str = "") -> str:
    if f.kind not in ("vk_wall", "tg_channel", "rss"):
        raise ValueError("это не источник")
    key = f.exists or f"{f.kind}:{f.handle}"
    cat = category or f.category
    ctx.db.upsert_source({
        "key": key, "kind": f.kind, "handle": f.handle, "title": title or f.title or f.handle,
        "tier": tier or f.tier, "region": region if region is not None else f.region, "category": cat,
        "comments": f.comments if comments is None else comments, "where": "colab" if f.kind == "tg_channel" else "server",
        "enabled": 1, "note": f"добавлено по ссылке {iso()[:10]}" + (f" ({by})" if by else "")})
    ctx.db.x("UPDATE sources SET candidate=0, enabled=1, members=?, verified=1 WHERE key=?", (f.members or 0, key))
    if f.uid is not None and f.kind == "vk_wall":   # id уже известен — сборщику не нужно его искать заново
        ctx.db.x("UPDATE sources SET meta_json=? WHERE key=?", (jdump({"owner_id": f.uid, "title": f.title}), key))
        # этот же паблик мог висеть в кандидатах под числовым адресом — убираем дубль
        ctx.db.x("DELETE FROM sources WHERE key=? AND key<>? AND candidate<>0", (f"vk_wall:{f.uid}", key))
    ctx.db.event("info", "sources", f"добавлен источник {key} ({CATEGORIES.get(cat, cat)}){' — ' + by if by else ''}")
    return key


def _load_watch() -> dict:
    return yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text("utf-8")) or {}


def default_aliases(name: str) -> list[str]:
    parts = name.split()
    if len(parts) == 2:
        first, last = parts
        return [f"{last} {first}", f"{first[0]}. {last}", last] if len(last) >= 5 else [f"{last} {first}"]
    return []


def add_person(ctx: "Ctx", found: list[Found], *, name: str, aliases: list[str] | None = None, note: str = "",
               by: str = "") -> dict:
    """Ставит человека на контроль (или добавляет ссылку к уже известному человеку с тем же именем)."""
    data = _load_watch()
    people: list[dict[str, Any]] = data.setdefault("people", []) or []
    person = next((p for p in people if p.get("name", "").lower() == name.lower()), None)
    if person is None:
        person = {"name": name, "aliases": [], "vk_id": "", "tg": "", "note": ""}
        people.append(person)
    for f in found:
        if f.platform == "vk":
            person["vk_id"], person["vk_uid"] = f.handle, f.uid
            person["vk_followers"] = f.members
        elif f.platform == "tg":
            person["tg"] = f.handle
            person["tg_channel"] = f.kind == "tg_channel"
    person["aliases"] = sorted({*person.get("aliases", []), *(aliases or [])} - {name})
    if note:
        person["note"] = note
    person.setdefault("added", f"{iso()[:10]}{' · ' + by if by else ''}")
    data["people"] = people
    ctx.save_watchlist(data)
    ctx.sync_sources_from_yaml()
    ctx.db.event("info", "watch", f"на контроле: {name}{' — ' + by if by else ''}")
    return person


def remove_person(ctx: "Ctx", name: str) -> bool:
    data = _load_watch()
    before = len(data.get("people", []) or [])
    keep = [p for p in data.get("people", []) or [] if p.get("name", "").lower() != name.lower()]
    gone = [p for p in data.get("people", []) or [] if p.get("name", "").lower() == name.lower()]
    data["people"] = keep
    ctx.save_watchlist(data)
    for p in gone:   # стену/канал человека больше не опрашиваем
        for kind, fld in (("vk_wall", "vk_id"), ("tg_channel", "tg")):
            if p.get(fld):
                ctx.db.x("UPDATE sources SET enabled=0 WHERE key=? AND category='influencer'", (f"{kind}:{p[fld]}",))
    return len(keep) < before


# ---------------------------------------------------------------------------------------------- рекомендованный набор
PACK_FILE = "recommended.yaml"
VK_DAILY_LIMIT = 5000          # ВК ограничивает wall.get ≈ 5000 вызовов в сутки на ключ
DEAD_DAYS = 120                # молчит дольше — не добавляем


def load_pack() -> list[dict]:
    """Плоский список рекомендаций: у каждой — группа, ссылка, почему, приоритет и настройки опроса."""
    path = CONFIG_DIR / PACK_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    out = []
    for g in data.get("groups", []) or []:
        base = {k: v for k, v in g.items() if k not in ("items", "name", "note")}
        for it in g.get("items", []) or []:
            out.append({"tier": "warm", "category": "public", "region": "", "comments": False, "prio": 2,
                        **base, **it, "group": g["name"], "group_note": g.get("note", "")})
    return out


def pack_state(ctx: "Ctx", item: dict) -> tuple[str, str]:
    """(статус, пояснение): added | exists | dead | error | new."""
    res = (ctx.db.kv_get("pack_results", {}) or {}).get(item["link"])
    if res and res.get("key") and ctx.db.one("SELECT 1 FROM sources WHERE key=? AND candidate=0", (res["key"],)):
        return "added", res.get("text", "")
    p = parse(item["link"])
    if not isinstance(p, str):
        kind = {"tg": "tg_channel", "vk": "vk_wall", "web": "rss"}[p[0]]
        if existing_key(ctx, kind, p[1]):
            return "exists", "уже отслеживается"
    if res and res.get("status") in ("dead", "error"):
        return res["status"], res.get("text", "")
    return "new", ""


def _tier_minutes(ctx: "Ctx", tier: str) -> int:
    sch = ctx.settings.schedule
    return {"hot": sch.hot_minutes, "warm": sch.warm_minutes}.get(tier, sch.cold_minutes) or 15


def vk_budget(ctx: "Ctx", extra: list[dict] = ()) -> int:
    """Сколько вызовов wall.get в сутки уйдёт на стены ВК (включённые + отмеченные в наборе)."""
    n = sum(1440 // _tier_minutes(ctx, r["tier"]) for r in ctx.db.q(
        "SELECT tier FROM sources WHERE kind='vk_wall' AND enabled=1 AND candidate=0"))
    return n + sum(1440 // _tier_minutes(ctx, it.get("tier", "warm")) for it in extra)


async def install_pack_item(ctx: "Ctx", service, item: dict, by: str = "") -> tuple[str, str]:
    """Проверить ссылку вживую и добавить. Мёртвое, закрытое и несуществующее не добавляется."""
    f = await preview(ctx, service, item["link"])
    if f.error:
        status, text, key = "error", human_error(f.error), None
    elif f.exists:
        status, text, key = "exists", "уже отслеживается", f.exists
    elif f.person or f.kind not in ("vk_wall", "tg_channel", "rss"):
        status, text, key = "error", "это страница человека — ставьте на контроль на вкладке «Наблюдение»", None
    elif f.last_at and f.last_at < ago(days=DEAD_DAYS):
        status, text, key = "dead", f"молчит с {msk(f.last_at, '%d.%m.%Y')} — не добавлен", None
    else:
        key = add_source(ctx, f, title=f.title or item.get("title"), category=item.get("category"),
                         tier=item.get("tier"), region=item.get("region") if item.get("region") is not None else f.region,
                         comments=bool(item.get("comments")) if f.kind == "vk_wall" else None,
                         by=by or "рекомендованный набор")
        # ссылка из набора + привязка к УК/ЖК (жалоба в группе ЖК — про этот ЖК, даже если название не написано)
        r = ctx.db.one("SELECT meta_json FROM sources WHERE key=?", (key,))
        meta = {**(jload(r["meta_json"], {}) if r else {}), "pack": item["link"],
                **{k: item[k] for k in ("company", "zhk") if item.get(k)}}
        ctx.db.x("UPDATE sources SET meta_json=?, note=? WHERE key=?", (jdump(meta), item.get("why", ""), key))
        status, text = "added", f.summary() + (" · ⚠️ " + "; ".join(f.notes) if f.notes else "")
    res = ctx.db.kv_get("pack_results", {}) or {}
    res[item["link"]] = {"status": status, "text": text, "key": key, "at": iso()}
    ctx.db.kv_set("pack_results", res)
    return status, text
