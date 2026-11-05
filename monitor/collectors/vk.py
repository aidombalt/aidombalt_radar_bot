"""ВКонтакте через официальный API: стены, КОММЕНТАРИИ (главный урок кейса), поиск по всей сети.

Типы ключей: сервисный ключ приложения (VK_SERVICE_TOKEN) — стены и комментарии публичных страниц;
пользовательский (VK_USER_TOKEN, по желанию) — поиск сообществ для автопоиска и пакетные запросы execute.
Комментарии под постами губернаторов поисковики не индексируют — поэтому только прямой опрос.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta

import httpx

from .. import candidates
from ..util import ago, clip, from_ts, iso, make_http, now_utc
from .base import BaseCollector, SourceError

log = logging.getLogger("radar.vk")

HOT_POST_RE = re.compile(r"прям\w*\s+лини|задавайте\s+(?:ваши\s+)?вопрос|прям\w*\s+эфир|жду\s+ваши\s+вопрос|ответ\w*\s+на\s+вопрос", re.I)


class VKError(SourceError):
    def __init__(self, code: int, msg: str):
        super().__init__(f"VK {code}: {msg}")
        self.code = code


class VKApi:
    def __init__(self, ctx):
        self.ctx = ctx
        cfg = ctx.settings.vk
        self.version, self.bases = cfg.api_version, cfg.api_bases
        self.service = ctx.settings.vk_service_token
        self.user = ctx.settings.vk_user_token
        self.min_interval = 1.0 / max(0.5, cfg.rps)
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.client = make_http(timeout=30)

    @property
    def ready(self) -> bool:
        return bool(self.service or self.user)

    async def _throttle(self):
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def _count(self, method: str) -> int:
        key = f"vkcalls:{now_utc():%Y-%m-%d}"
        d = self.ctx.db.kv_get(key, {}) or {}
        d[method] = d.get(method, 0) + 1
        self.ctx.db.kv_set(key, d)
        return d[method]

    def calls_today(self, method: str) -> int:
        return (self.ctx.db.kv_get(f"vkcalls:{now_utc():%Y-%m-%d}", {}) or {}).get(method, 0)

    async def call(self, method: str, params: dict, user: bool = False):
        token = (self.user if user else (self.service or self.user))
        if not token:
            raise SourceError("нет токена VK" + (" (нужен пользовательский)" if user else ""))
        data = {**{k: v for k, v in params.items() if v is not None}, "access_token": token, "v": self.version}
        for attempt in range(4):
            await self._throttle()
            last_exc = None
            for base in self.bases:
                try:
                    r = await self.client.post(f"{base}/{method}", data=data)
                    break
                except httpx.HTTPError as e:
                    last_exc = e
                    continue
            else:
                raise SourceError(f"VK недоступен: {last_exc}")
            self._count(method)
            j = r.json()
            if "error" in j:
                code, msg = j["error"].get("error_code"), j["error"].get("error_msg", "")
                if code in (6, 10) and attempt < 3:        # слишком часто / внутренняя ошибка
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                if code == 5:
                    self.ctx.db.event("error", "vk", f"токен VK недействителен: {msg}")
                if code == 29:     # суточный лимит метода (wall.get ≈ 5000/сутки на ключ) — сбросится в полночь МСК
                    k = f"vk29:{now_utc():%Y-%m-%d}:{method}"
                    if not self.ctx.db.kv_get(k):
                        self.ctx.db.kv_set(k, 1)
                        self.ctx.db.event("error", "vk", f"ВК: исчерпан суточный лимит {method} "
                                          f"({self.calls_today(method)} вызовов). Сократите частоту ВК-источников "
                                          "(«Источники» → «каждые 15 мин») или часть отключите.")
                if code == 1051:   # ключ приложения VK ID: для методов API ВК не годится
                    msg += " → ключ от приложения VK ID не работает с API ВК, нужен сервисный ключ мини-приложения " \
                           "(dev.vk.ru → Приложения → Создать → «Мини-приложение»)"
                    self.ctx.db.event("error", "vk", msg)
                raise VKError(code, msg)
            return j.get("response")
        raise SourceError("VK: исчерпаны повторы")


def _photo_url(att: dict) -> str | None:
    ph = att.get("photo") or {}
    sizes = sorted(ph.get("sizes", []), key=lambda s: s.get("width", 0) * s.get("height", 0))
    return sizes[-1]["url"] if sizes else None


def _post_text(p: dict) -> str:
    parts = [p.get("text") or ""]
    for a in p.get("attachments", []) or []:
        if a.get("type") == "link":
            parts.append(f"[ссылка] {a['link'].get('title', '')} {a['link'].get('url', '')}")
        elif a.get("type") == "video":
            parts.append(f"[видео] {a['video'].get('title', '')}")
    for ch in p.get("copy_history", []) or []:   # кейс Лампо: суть была в репосте с видео, а не в тексте автора
        parts.append(f"[репост] {ch.get('text', '')}")
        for a in ch.get("attachments", []) or []:
            if a.get("type") == "video":
                v = a["video"]
                parts.append(f"[видео {v.get('duration', 0) // 60} мин] {v.get('title', '')} {v.get('description', '')}".strip())
    return "\n".join(x for x in parts if x).strip()


def _media(p: dict) -> list[str]:
    urls = [_photo_url(a) for a in (p.get("attachments") or []) if a.get("type") == "photo"]
    for ch in p.get("copy_history", []) or []:
        urls += [_photo_url(a) for a in (ch.get("attachments") or []) if a.get("type") == "photo"]
    return [u for u in urls if u][:3]


class Profiles:
    def __init__(self, resp: dict | None):
        self.users = {u["id"]: u for u in (resp or {}).get("profiles", []) or []}
        self.groups = {g["id"]: g for g in (resp or {}).get("groups", []) or []}

    def name(self, oid: int) -> str:
        if oid > 0 and oid in self.users:
            u = self.users[oid]
            return f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
        if oid < 0 and -oid in self.groups:
            return self.groups[-oid].get("name", "")
        return str(oid)

    def reach(self, oid: int) -> int:
        if oid > 0 and oid in self.users:
            return self.users[oid].get("followers_count", 0) or 0
        if oid < 0 and -oid in self.groups:
            return self.groups[-oid].get("members_count", 0) or 0
        return 0


class VKCollector(BaseCollector):
    kind = "vk_wall"
    timeout = 240.0
    parallel = 3

    def __init__(self, ctx):
        super().__init__(ctx)
        self.api = VKApi(ctx)

    # ---------------------------------------------------------------- разрешение адреса страницы
    async def resolve(self, src: dict) -> dict:
        meta = src.get("meta") or {}
        if meta.get("owner_id"):
            return meta
        h = str(src["handle"]).strip().rstrip("/").split("/")[-1]
        oid = None
        if re.fullmatch(r"-?\d+", h):
            oid = int(h)
        elif m := re.fullmatch(r"id(\d+)", h):
            oid = int(m.group(1))
        elif m := re.fullmatch(r"(?:club|public|event)(\d+)", h):
            oid = -int(m.group(1))
        else:
            r = await self.api.call("utils.resolveScreenName", {"screen_name": h})
            if not r:
                raise SourceError(f"страница «{h}» не найдена")
            oid = r["object_id"] if r["type"] == "user" else -r["object_id"]
        meta["owner_id"] = oid
        try:
            if oid < 0:
                g = await self.api.call("groups.getById", {"group_id": -oid, "fields": "members_count"})
                g = (g.get("groups") if isinstance(g, dict) else g)[0]
                meta.update(title=g.get("name"), members=g.get("members_count", 0), closed=g.get("is_closed", 0))
            else:
                u = (await self.api.call("users.get", {"user_ids": oid, "fields": "followers_count"}))[0]
                meta.update(title=f"{u.get('first_name')} {u.get('last_name')}", members=u.get("followers_count", 0))
        except Exception as e:  # заголовок — не критично
            log.debug("vk meta %s: %s", h, e)
        self.save_meta(src["key"], meta)
        self.ctx.db.x("UPDATE sources SET verified=1, members=?, title=COALESCE(NULLIF(title,''),?) WHERE key=?",
                      (meta.get("members", 0), meta.get("title"), src["key"]))
        return meta

    # ---------------------------------------------------------------- стена + комментарии
    async def fetch(self, src: dict) -> list[dict]:
        if not self.api.ready:
            raise SourceError("VK_SERVICE_TOKEN не задан")
        meta = await self.resolve(src)
        oid = meta["owner_id"]
        n = self.ctx.settings.vk.posts_per_wall
        resp = await self.api.call("wall.get", {"owner_id": oid, "count": n, "extended": 1,
                                                "fields": "followers_count,members_count"})
        prof = Profiles(resp)
        posts = resp.get("items", []) if resp else []
        items = []
        for p in posts:
            author = p.get("signer_id") or p.get("from_id") or oid
            items.append({
                "platform": "vk_post", "external_id": f"vk:{oid}_{p['id']}", "url": f"https://vk.ru/wall{oid}_{p['id']}",
                "author_id": f"vk:{author}", "author_name": prof.name(author) or meta.get("title"),
                "author_followers": prof.reach(author) or meta.get("members", 0), "text": _post_text(p),
                "published_at": from_ts(p.get("date", time.time())), "likes": (p.get("likes") or {}).get("count", 0),
                "comments": (p.get("comments") or {}).get("count", 0), "reposts": (p.get("reposts") or {}).get("count", 0),
                "views": (p.get("views") or {}).get("count", 0), "media": _media(p),
            })
        if src.get("comments"):
            items += await self._comments(src, oid, posts)
        return items

    async def _comments(self, src: dict, oid: int, posts: list[dict]) -> list[dict]:
        db, out = self.ctx.db, []
        lookback = iso(now_utc() - timedelta(hours=self.ctx.settings.vk.comments_lookback_hours))
        for p in posts:
            pub = from_ts(p.get("date", 0))
            ckey = f"{oid}_{p['id']}"
            hot_until = db.cursor_get(f"vk:hot:{ckey}")
            is_hot = bool(hot_until and hot_until > iso())
            if pub < lookback and not is_hot:
                continue
            if HOT_POST_RE.search(p.get("text") or "") and not hot_until:
                db.cursor_set(f"vk:hot:{ckey}", iso(now_utc() + timedelta(hours=36)))
                db.event("info", "vk", f"прямая линия/сбор вопросов: vk.com/wall{ckey} — усиленный опрос 36 ч")
                is_hot = True
            count = (p.get("comments") or {}).get("count", 0)
            if count <= int(db.cursor_get(f"vk:cc:{ckey}", "0") or 0):
                continue
            last_seen = int(db.cursor_get(f"vk:cl:{ckey}", "0") or 0)
            parent_text = clip(_post_text(p), 1200)
            max_pages = 20 if is_hot else 5
            newest = last_seen
            for page in range(max_pages):
                try:
                    resp = await self.api.call("wall.getComments", {
                        "owner_id": oid, "post_id": p["id"], "count": 100, "offset": page * 100, "sort": "desc",
                        "need_likes": 1, "extended": 1, "thread_items_count": 10, "fields": "followers_count,members_count"})
                except VKError as e:
                    # 15/18/30/212: пост удалён, комментарии закрыты или скрыты — пропускаем пост, а не весь источник
                    if e.code not in (15, 18, 30, 212):
                        raise
                    log.info("комментарии %s недоступны (%s) — пропускаю пост", ckey, e)
                    resp = None
                prof = Profiles(resp)
                batch = resp.get("items", []) if resp else []
                stop = False
                for c in batch:
                    if c["id"] <= last_seen:
                        stop = True
                        break
                    newest = max(newest, c["id"])
                    out.append(self._comment_item(c, oid, p["id"], parent_text, prof))
                    root = clip(c.get("text") or "", 600)
                    ctx_text = f"{parent_text}\n\n[ответ в ветке комментария] {root}" if root else parent_text
                    for t in (c.get("thread") or {}).get("items", []) or []:
                        if t["id"] > last_seen:
                            out.append(self._comment_item(t, oid, p["id"], ctx_text, prof, thread=c["id"]))
                            newest = max(newest, t["id"])
                if stop or len(batch) < 100:
                    break
            db.cursor_set(f"vk:cc:{ckey}", count)
            db.cursor_set(f"vk:cl:{ckey}", newest)
        return out

    @staticmethod
    def _comment_item(c: dict, oid: int, pid: int, parent_text: str, prof: Profiles, thread: int | None = None) -> dict:
        author = c.get("from_id", 0)
        url = f"https://vk.ru/wall{oid}_{pid}?reply={c['id']}" + (f"&thread={thread}" if thread else "")
        text = c.get("text") or ""
        for a in c.get("attachments", []) or []:
            if a.get("type") == "photo":
                text += " [фото]"
        return {
            "platform": "vk_comment", "external_id": f"vk:{oid}_{pid}_{c['id']}", "url": url,
            "author_id": f"vk:{author}", "author_name": prof.name(author), "author_followers": prof.reach(author),
            "parent_external_id": f"vk:{oid}_{pid}", "parent_text": parent_text, "text": text.strip(),
            "published_at": from_ts(c.get("date", time.time())), "likes": (c.get("likes") or {}).get("count", 0),
            "media": [u for u in (_photo_url(a) for a in (c.get("attachments") or []) if a.get("type") == "photo") if u][:2],
        }


class VKSearchCollector(BaseCollector):
    """newsfeed.search по всей ВК: ротация запросов в рамках суточного бюджета, без пропусков по времени."""
    kind = "vk_search"
    timeout = 300.0
    parallel = 1

    def __init__(self, ctx, api: VKApi):
        super().__init__(ctx)
        self.api = api

    def plan(self, queries: list[dict]) -> list[dict]:
        budget = self.ctx.settings.vk.search_daily_budget
        used = self.api.calls_today("newsfeed.search")
        if used >= budget:
            return []
        cycles_left = max(1, int((24 * 60) / max(5, self.ctx.settings.schedule.warm_minutes)))
        per_cycle = max(3, min(30, (budget - used) // cycles_left + 1))
        pri = [q for q in queries if q.get("priority")]
        rest = sorted((q for q in queries if not q.get("priority")),
                      key=lambda q: self.ctx.db.cursor_get(f"vks:{q['q']}", "") or "")
        return (pri + rest)[:per_cycle]

    async def fetch(self, src: dict) -> list[dict]:
        q = src["handle"]
        since = self.ctx.db.cursor_get(f"vks:{q}") or ago(hours=24)
        start_ts = int(max(time.time() - 3 * 86400, _ts(since) - 600))
        items, next_from = [], None
        for _ in range(3):
            resp = await self.api.call("newsfeed.search", {
                "q": q, "count": 200, "extended": 1, "start_time": start_ts, "start_from": next_from,
                "fields": "followers_count,members_count"})
            prof = Profiles(resp)
            for p in (resp or {}).get("items", []):
                oid, author = p.get("owner_id"), p.get("signer_id") or p.get("from_id") or p.get("owner_id")
                # «снежный ком»: паблик, где пост ДОКАЗУЕМО про наш объект, — кандидат в постоянные источники
                if oid and oid < 0:
                    ev = candidates.evidence(self.ctx, _post_text(p), f"https://vk.ru/wall{oid}_{p['id']}",
                                             from_ts(p.get("date", time.time())))
                    if ev:
                        candidates.propose(self.ctx, oid, prof.groups.get(-oid, {}), ev)
                items.append({
                    "platform": "vk_post", "external_id": f"vk:{oid}_{p['id']}", "url": f"https://vk.ru/wall{oid}_{p['id']}",
                    "author_id": f"vk:{author}", "author_name": prof.name(author),
                    "author_followers": max(prof.reach(author), prof.reach(oid)), "text": _post_text(p),
                    "published_at": from_ts(p.get("date", time.time())),
                    "likes": (p.get("likes") or {}).get("count", 0), "comments": (p.get("comments") or {}).get("count", 0),
                    "reposts": (p.get("reposts") or {}).get("count", 0), "views": (p.get("views") or {}).get("count", 0),
                    "media": _media(p),
                })
            next_from = (resp or {}).get("next_from")
            if not next_from:
                break
        self.ctx.db.cursor_set(f"vks:{q}", iso())
        return items


def _ts(s: str) -> float:
    from ..util import parse_iso

    d = parse_iso(s)
    return d.timestamp() if d else time.time() - 86400


def build_search_queries(ctx) -> list[dict]:
    """Запросы для поиска по ВК из справочника: названия + «ЖК …» + соседи + кластер×«УК»."""
    reg, qs = ctx.reg, []
    for c in reg.companies.values():
        for al in c.get("aliases", []):
            if al.upper().startswith("УК ") or not c.get("ambiguous"):
                qs.append({"q": al, "priority": al.upper().startswith("УК ")})
    for z in reg.zhks.values():
        name = z["name"]
        qs.append({"q": f"ЖК {name}", "priority": False})
        if not z.get("ambiguous") and name not in ("Москва",):
            qs.append({"q": name, "priority": False})
    for n in reg.neighbors:
        qs += [{"q": a, "priority": i < 4} for i, a in enumerate(n.get("aliases", [])[:5])]
    for cl in reg.clusters:
        for kw in cl.get("keywords", [])[:1]:
            qs += [{"q": f"{kw} управляющая компания", "priority": cl.get("region") == "LO"},
                   {"q": f"{kw} УК", "priority": False}]
    for q in ("Дрозденко управляющая", "Дрозденко Мурино", "Дрозденко УК", "Госжилнадзор Мурино",
              "Беглов управляющая компания", "ГЖИ управляющая компания"):
        qs.append({"q": q, "priority": q.startswith("Дрозденко")})
    for p in ctx.watch.people:
        qs.append({"q": p["name"], "priority": True})
    for kw in ctx.watch.keywords:
        qs.append({"q": kw, "priority": False})
    seen, out = set(), []
    for q in qs:
        if q["q"].lower() not in seen:
            seen.add(q["q"].lower())
            out.append(q)
    return out


async def discover_groups(ctx, api: VKApi, queries: list[str] | None = None) -> int:
    """Автопоиск локальных пабликов (нужен пользовательский токен): Мурино, Кудрово, названия ЖК, районы."""
    if not api.user:
        return 0
    queries = queries or ([kw for cl in ctx.reg.clusters for kw in cl.get("keywords", [])[:2]] +
                          [f"ЖК {z['name']}" for z in ctx.reg.zhks.values()])
    added = 0
    for q in queries:
        try:
            r = await api.call("groups.search", {"q": q, "count": 20, "type": "group,page"}, user=True)
        except Exception as e:
            log.info("groups.search %s: %s", q, e)
            continue
        ids = [g["id"] for g in (r or {}).get("items", [])]
        if not ids:
            continue
        info = await api.call("groups.getById", {"group_ids": ",".join(map(str, ids)), "fields": "members_count"})
        groups = info.get("groups") if isinstance(info, dict) else info
        for g in groups or []:
            key = f"vk_wall:-{g['id']}"
            if (g.get("members_count", 0) or 0) < 500 or ctx.db.one("SELECT 1 FROM sources WHERE key=?", (key,)):
                continue
            ctx.db.upsert_source({"key": key, "kind": "vk_wall", "handle": f"-{g['id']}", "title": g.get("name"),
                                  "tier": "warm", "category": "public", "candidate": 1,
                                  "members": g.get("members_count", 0), "note": f"автопоиск: «{q}»",
                                  "meta": {"owner_id": -g["id"], "title": g.get("name"), "members": g.get("members_count", 0)}})
            added += 1
    ctx.db.event("info", "discovery", f"автопоиск ВК: новых кандидатов {added}")
    return added
