"""Ссылки Google News → оригинал статьи. news.google.com в РФ закрыт — в алерте и отчёте нужна ссылка на сам сайт.

Порядок (от дешёвого к дорогому):
  1) старые id Google (CBMi…) несут адрес прямо внутри — раскодировать без сети;
  2) та же статья уже есть в базе из RSS самого издания — берём её адрес;
  3) релей Cloudflare (v5, путь /gn-resolve/<id>) спрашивает у Google оригинал — Google режет РФ, а релей за рубежом;
  4) не вышло — ссылка на поиск Яндекса «заголовок + сайт»: в РФ работает всегда, оригинал первым в выдаче.
Нерасшифрованные дорешивает фоновая задача (раз в 10 мин), дочитывает полный текст и обновляет карточку алерта.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING

from .util import ago, relay_url

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger("radar.links")
GN_RE = re.compile(r"^https?://news\.google\.com/(?:rss/)?(?:articles|read)/([A-Za-z0-9_\-]{16,})")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{24,}$")
SEARCH_PREFIX = "https://yandex.ru/search/?text="


def gn_id(url: str | None) -> str | None:
    m = GN_RE.match(url or "")
    return m.group(1) if m else None


def gn_id_from_item(item: dict) -> str | None:
    """id статьи Google: из ссылки, а если ссылку уже заменили на поиск — из external_id (там guid Google)."""
    gid = gn_id(item.get("url"))
    if gid:
        return gid
    tail = str(item.get("external_id") or "").split(":", 1)[-1]
    return gn_id(tail) or (tail if ID_RE.match(tail) and tail.startswith("CB") else None)


def decode_offline(gid: str) -> str | None:
    """Id старого формата (до 2024) содержит адрес статьи внутри base64."""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except (ValueError, TypeError):
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    url = m.group(0).decode("ascii", "ignore")
    host = urllib.parse.urlsplit(url).hostname or ""
    return url if host and "google." not in host and "AU_yqL" not in url else None


def split_title(title: str) -> tuple[str, str]:
    """«Заголовок - Петербургский дневник» → (заголовок, издание)."""
    m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]{2,60})$", (title or "").strip())
    return (m.group(1), m.group(2)) if m else ((title or "").strip(), "")


def search_url(title: str, source_url: str | None = None) -> str:
    head, _ = split_title(title)
    host = (urllib.parse.urlsplit(source_url or "").hostname or "").removeprefix("www.")
    q = f'"{head[:150]}"' + (f" site:{host}" if host else "")
    return SEARCH_PREFIX + urllib.parse.quote(q)


def is_placeholder(url: str | None) -> bool:
    """Ссылка ещё не на оригинал (Google или поиск)."""
    return bool(url) and (url.startswith(SEARCH_PREFIX) or bool(gn_id(url)))


def from_db(ctx: "Ctx", title: str) -> str | None:
    """Та же статья уже пришла напрямую из RSS издания."""
    head, _ = split_title(title)
    if len(head) < 25:
        return None
    r = ctx.db.one("SELECT url FROM items WHERE published_at>? AND platform='rss' AND url NOT LIKE 'https://news.google.com/%' "
                   "AND url NOT LIKE ? AND (title=? OR title LIKE ?) LIMIT 1",
                   (ago(days=7), SEARCH_PREFIX + "%", head, head[:80] + "%"))
    return r["url"] if r else None


async def resolve_remote(ctx: "Ctx", client, gid: str) -> str | None:
    """Через релей v5: он открывает страницу Google и забирает оригинал. 503/429 — общая пауза Google News."""
    from .collectors.web import gnews_limited, gnews_pause

    relay = relay_url()
    if not relay or gnews_pause(ctx):
        return None
    r = await client.get(f"{relay}/gn-resolve/{gid}", timeout=30)
    if r.status_code in (429, 503):
        gnews_limited(ctx, r.status_code)
        return None
    if r.status_code == 404:          # релей старый (без /gn-resolve)
        return None
    try:
        data = r.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("status") in (429, 503):
        gnews_limited(ctx, data["status"])
    url = data.get("url") or ""
    return url if url.startswith("http") and not gn_id(url) else None


async def resolve(ctx: "Ctx", client, url: str, title: str, source_url: str | None, remote: bool = True) -> tuple[str, str]:
    """(ссылка, как получена): original | db | relay | search."""
    gid = gn_id(url)
    if not gid:
        return url, "original"
    found = decode_offline(gid)
    if found:
        return found, "offline"
    found = from_db(ctx, title)
    if found:
        return found, "db"
    if remote:
        try:
            found = await resolve_remote(ctx, client, gid)
        except Exception as e:  # noqa: BLE001 — сеть; дорешит фоновая задача
            log.debug("gn-resolve %s: %s", gid[:20], e)
        if found:
            return found, "relay"
    return search_url(title, source_url), "search"


async def fix_items(ctx: "Ctx", client, items: list[dict], budget: int = 5) -> None:
    """Сборщики Google News: заменить ссылки на оригиналы ДО анализа и алерта (новые — с запросом, старые — из базы)."""
    for it in items:
        if not gn_id(it.get("url")):
            continue
        known = ctx.db.one("SELECT url FROM items WHERE platform=? AND external_id=?", (it["platform"], it["external_id"]))
        if known and known["url"] and not gn_id(known["url"]):
            it["url"] = known["url"]
            continue
        # запрос к Google (через релей) — только для того, что пойдёт в анализ; остальному хватит ссылки поиска
        worth = budget > 0 and ctx.matcher.match(f"{it.get('title') or ''} {it.get('text') or ''}").candidate
        it["url"], how = await resolve(ctx, client, it["url"], it.get("title") or "", it.get("source_url"), remote=worth)
        if how == "relay":
            budget -= 1


async def sweep(ctx: "Ctx", client, article_client=None, limit: int = 5) -> int:
    """Фоновая дорешка: значимые публикации со ссылкой-заглушкой → оригинал, полный текст, свежая карточка."""
    from .collectors import portable

    rows = ctx.db.q("SELECT * FROM items WHERE published_at>? AND (url LIKE 'https://news.google.com/%' OR url LIKE ?) "
                    "AND (candidate=1 OR priority IN ('P1','P2','P3')) ORDER BY priority, published_at DESC LIMIT 30",
                    (ago(days=3), SEARCH_PREFIX + "%"))
    done = 0
    for r in rows:
        if done >= limit:
            break
        item = dict(r)
        tries = int(ctx.db.kv_get(f"gnres:{item['id']}", 0) or 0)
        if tries >= 4:
            continue
        ctx.db.kv_set(f"gnres:{item['id']}", tries + 1)
        gid = gn_id_from_item(item)
        new = None
        if gid:
            new = decode_offline(gid) or from_db(ctx, item.get("title") or "")
            if not new:
                try:
                    new = await resolve_remote(ctx, client, gid)
                except Exception as e:  # noqa: BLE001
                    log.debug("gn-resolve %s: %s", gid[:20], e)
        if not new:
            if gn_id(item["url"]):          # хотя бы убрать закрытую ссылку Google
                ctx.db.x("UPDATE items SET url=? WHERE id=?", (search_url(item.get("title") or "", None), item["id"]))
            continue
        done += 1
        ctx.db.x("UPDATE items SET url=? WHERE id=?", (new, item["id"]))
        ctx.db.event("info", "links", f"#{item['id']}: ссылка Google News заменена оригиналом {new[:120]}")
        # полный текст статьи с самого сайта (сайты РФ с ноутбука открываются) — анализ по статье, а не по анонсу
        if article_client is not None:
            try:
                page = await article_client.get(new, timeout=25)
                body = portable.extract_article(page.text) if page.status_code == 200 else ""
                if len(body) > len(item.get("text") or "") + 200:
                    src = ctx.db.one("SELECT * FROM sources WHERE key=?", (item["source_key"],))
                    ctx.pipeline.ingest([{**{k: item[k] for k in ("platform", "external_id", "author_id", "author_name",
                                                                  "published_at", "title")}, "url": new, "text": body[:20000]}],
                                        dict(src) if src else {"key": item["source_key"], "category": "media"})
            except Exception as e:  # noqa: BLE001
                log.debug("статья %s: %s", new[:80], e)
        if ctx.db.one("SELECT 1 FROM alert_msgs WHERE item_id=?", (item["id"],)):
            ctx.alerts.schedule_refresh(item["id"])
    return done
