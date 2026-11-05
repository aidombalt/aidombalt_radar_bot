"""Сборщики «веба» на сервере: RSS СМИ (с автопоиском ленты и дочиткой статей), Telegram-каналы,
Google News, произвольные страницы (госпорталы) по CSS-селекторам.

Где запускать: источники с where=colab (Telegram, Google News, заблокированные в РФ СМИ) опрашивает Colab
(он за рубежом), с where=server — сервер в РФ (госсайты с гео-блоком иностранных IP, российские СМИ).
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse

import httpx
from selectolax.parser import HTMLParser

from ..matcher import HOUSING
from ..textnorm import lemmas
from datetime import timedelta

from ..util import ago, iso, make_http, msk, now_utc, relay_url
from .. import links
from . import portable
from .base import BaseCollector, SourceError, SourcePaused

log = logging.getLogger("radar.web")
RECHECK = {"hot": 4, "warm": 2}      # сколько последних постов канала перечитывать каждый опрос
RELAY_MIN = 5                         # минимальная версия релея (deploy/cloudflare_worker.js)
GNEWS_PER_CYCLE = 8                   # Google News режет частые запросы с адреса релея: не больше 8 за цикл
GNEWS_SITE_EVERY_MIN = 60             # издания без RSS (site:…) — не чаще раза в час


# ---------------------------------------------------------------- Google News: общая пауза при ограничении
def gnews_pause(ctx) -> str | None:
    """До какого времени Google News на паузе (он ответил 503/429), или None."""
    st = ctx.db.kv_get("gnews_pause", {}) or {}
    return st.get("until") if st.get("until") and st["until"] > iso() else None


def gnews_limited(ctx, code: int) -> str:
    st = ctx.db.kv_get("gnews_pause", {}) or {}
    step = min(int(st.get("step") or 0) + 1, 4)
    until = iso(now_utc() + timedelta(minutes=30 * 2 ** (step - 1)))       # 30 мин → 1 ч → 2 ч → 4 ч
    ctx.db.kv_set("gnews_pause", {"step": step, "until": until, "since": st.get("since") or iso(), "code": code})
    if step == 1:
        ctx.db.event("warning", "gnews", f"Google News ограничил запросы (HTTP {code}) — пауза до {msk(until)}. "
                                         "СМИ без RSS временно не читаются; каналы, ВК и RSS работают.")
    log.warning("Google News: HTTP %s — пауза до %s (шаг %s)", code, msk(until), step)
    return until


def gnews_ok(ctx) -> None:
    st = ctx.db.kv_get("gnews_pause", {}) or {}
    if st.get("step"):
        ctx.db.kv_set("gnews_pause", {"step": 0, "until": None, "ok_at": iso()})
        ctx.db.event("info", "gnews", "Google News снова отвечает")


async def gnews_get(ctx, client, url: str):
    """Запрос к Google News через общую «заслонку»: на паузе — не стучимся; 503/429 — пауза для всех запросов."""
    until = gnews_pause(ctx)
    if until:
        raise SourcePaused(f"Google News на паузе до {msk(until)}")
    r = await client.get(url)
    if r.status_code in (429, 503):
        raise SourcePaused(f"Google News ограничил запросы до {msk(gnews_limited(ctx, r.status_code))}")
    if r.status_code < 400:
        gnews_ok(ctx)
    return r

# Для изданий без RSS: что искать на их сайте через Google News
SITE_QUERY = ('Мурино OR Кудрово OR Девяткино OR Сертолово OR "управляющая компания" OR "управляющей компании" '
              'OR ЖКХ OR жильцы OR "Балтийский дом"')

PREFILTER = re.compile(r"мурин|кудров|управляющ|\bук\b|жкх|жк\b|жилой комплекс|жильц|жител|двор|подъезд|лифт|"
                       r"отоплен|коммунал|гжи|жилнадзор|жилинспек", re.I)


class RSSCollector(BaseCollector):
    kind = "rss"
    parallel = 6

    def __init__(self, ctx, proxy: str | None = None):
        super().__init__(ctx)
        self.client = make_http(proxy=proxy)

    async def fetch(self, src: dict) -> list[dict]:
        meta = src.get("meta") or {}
        url = src["handle"]
        feed = meta.get("feed") or (url if re.search(r"rss|feed|\.xml|atom", url, re.I) else None)
        if not feed:  # дали главную страницу — ищем ленту сами и запоминаем
            feed = await portable.discover_rss(self.client, url)
            if not feed:  # ленты нет — статьи издания берём из Google News с фильтром site: (через релей)
                host = urllib.parse.urlsplit(url).hostname or url
                feed = portable.gnews_url(f"site:{host.removeprefix('www.')} ({SITE_QUERY})", "2d")
                meta["via_gnews"] = True
                log.info("%s: RSS нет — читаю издание через Google News (site:%s)", src["key"], host)
            meta["feed"] = feed
            self.save_meta(src["key"], meta)
        headers = {}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("modified"):
            headers["If-Modified-Since"] = meta["modified"]
        if meta.get("via_gnews"):
            if meta.get("gn_at") and meta["gn_at"] > ago(minutes=GNEWS_SITE_EVERY_MIN):
                return []
            r = await gnews_get(self.ctx, self.client, feed)
            meta["gn_at"] = iso()
        else:
            r = await self.client.get(feed, headers=headers)
        if r.status_code == 304:
            return []
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code}")
        meta.update(etag=r.headers.get("etag"), modified=r.headers.get("last-modified"))
        self.save_meta(src["key"], meta)
        entries = portable.parse_feed(r.content, src["key"])
        items = portable.feed_to_items(entries, src["key"])
        if meta.get("via_gnews"):            # ссылки Google News в РФ не открываются — сразу на оригиналы
            await links.fix_items(self.ctx, self.client, items)
        # дочитка полного текста только для «подозрительных» статей (экономим трафик и время)
        if src.get("fulltext", True) and not meta.get("via_gnews"):
            for it in items[:40]:
                if self.ctx.db.one("SELECT 1 FROM items WHERE platform='rss' AND external_id=?", (it["external_id"],)):
                    continue
                if PREFILTER.search(f"{it['title']} {it['text']}") and it.get("url"):
                    try:
                        page = await self.client.get(it["url"])
                        if page.status_code == 200:
                            body = portable.extract_article(page.text)
                            if len(body) > len(it["text"]):
                                it["text"] = body
                    except Exception as e:
                        log.debug("fulltext %s: %s", it["url"], e)
        return items


class TGWebCollector(BaseCollector):
    kind = "tg_channel"
    parallel = 4

    def __init__(self, ctx, proxy: str | None = None):
        super().__init__(ctx)
        self.client = make_http(proxy=proxy)
        if relay_url() and not proxy:
            self.parallel = 2          # t.me не любит пачку одновременных запросов с одного адреса Cloudflare

    async def fetch(self, src: dict) -> list[dict]:
        handle = src["handle"].lstrip("@")
        since = int(self.ctx.db.cursor_get(f"tg:{handle}", "0") or 0)
        recheck = RECHECK.get(src.get("tier") or "", 0)     # последние посты перечитываем: правки и просмотры
        try:
            items, info = await portable.fetch_tg_channel(self.client, handle, since, recheck=recheck)
        except httpx.TimeoutException:  # один повтор: обычно это разовый затык по дороге
            await asyncio.sleep(3)
            items, info = await portable.fetch_tg_channel(self.client, handle, since, recheck=recheck)
        if info.get("subscribers"):
            self.ctx.db.x("UPDATE sources SET members=?, verified=1 WHERE key=?", (info["subscribers"], src["key"]))
        self._check_relay(info.get("relay") or "")
        if items:
            self.ctx.db.cursor_set(f"tg:{handle}", max(since, max(i["msg_id"] for i in items)))
        for i in items:
            if i.get("forwarded_from"):
                i["text"] = f"[переслано из: {i['forwarded_from']}] {i['text']}"
        return items


    def _check_relay(self, slim: str) -> None:
        """Релей v3 и старше в ответах на посты отдаёт цитату вместо текста поста — просим обновить (раз в сутки)."""
        m = re.match(r"v(\d+)", slim)
        if not m or int(m.group(1)) >= RELAY_MIN:
            return
        key = f"relay_old:{now_utc():%Y%m%d}"
        if not self.ctx.db.kv_get(key):
            self.ctx.db.kv_set(key, 1)
            why = ["ссылки Google News не превращаются в адреса статей (в РФ Google закрыт)"]
            if int(m.group(1)) < 4:
                why.insert(0, "в ответах на посты Telegram читается цитата вместо самого текста")
            msg = (f"Релей Cloudflare устарел (v{m.group(1)}, нужна v{RELAY_MIN}): " + "; ".join(why) + ". Обновите код "
                   "воркера из deploy/cloudflare_worker.js (Edit code → вставить → вернуть свою строку KEY → Deploy).")
            self.ctx.db.event("warning", "relay", msg)
            self.ctx.alerts.admin_notice("🔧 " + msg, key)


class GNewsCollector(BaseCollector):
    kind = "gnews"
    parallel = 2

    def plan(self, sources: list[dict]) -> list[dict]:
        """По кругу, не больше GNEWS_PER_CYCLE запросов за цикл: сначала те, что давно не спрашивали."""
        if gnews_pause(self.ctx):
            return []
        return sorted(sources, key=lambda s: self.ctx.db.cursor_get(f"gn:{s['handle']}", "") or "")[:GNEWS_PER_CYCLE]

    def __init__(self, ctx, proxy: str | None = None):
        super().__init__(ctx)
        self.client = make_http(proxy=proxy)

    async def fetch(self, src: dict) -> list[dict]:
        r = await gnews_get(self.ctx, self.client, portable.gnews_url(src["handle"]))
        self.ctx.db.cursor_set(f"gn:{src['handle']}", iso())
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code}")
        items = portable.feed_to_items(portable.parse_feed(r.content), src["key"], platform="gnews")
        await links.fix_items(self.ctx, self.client, items)      # в РФ news.google.com закрыт — даём оригиналы
        return items


class WebWatchCollector(BaseCollector):
    """Любая страница со списком записей: meta = {item: css, title: css, text: css, link: css}."""
    kind = "web"
    parallel = 3

    def __init__(self, ctx, proxy: str | None = None):
        super().__init__(ctx)
        self.client = make_http(proxy=proxy)

    async def fetch(self, src: dict) -> list[dict]:
        meta = src.get("meta") or {}
        if not meta.get("item"):
            raise SourceError("не заданы CSS-селекторы (meta.item/title/text/link) — настройте в GUI → Источники")
        r = await self.client.get(src["handle"])
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code}")
        tree = HTMLParser(r.text)
        out = []
        for node in tree.css(meta["item"])[:60]:
            t = node.css_first(meta.get("title", "a"))
            x = node.css_first(meta["text"]) if meta.get("text") else node
            a = node.css_first(meta.get("link", "a[href]"))
            href = urllib.parse.urljoin(src["handle"], a.attributes.get("href", "")) if a else src["handle"]
            text = x.text(separator=" ", strip=True) if x else ""
            if not text:
                continue
            out.append({"platform": "web", "external_id": f"web:{href}#{hash(text) & 0xffffffff}", "url": href,
                        "title": t.text(strip=True) if t else "", "text": text,
                        "author_id": f"web:{src['key']}", "author_name": src.get("title")})
        return out


def looks_housing(text: str) -> bool:
    return bool(set(lemmas(text)) & HOUSING)
