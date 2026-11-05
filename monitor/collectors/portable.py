"""Переносимые парсеры (сервер и Colab): Telegram t.me/s, RSS/Atom, Google News RSS, HTML-статьи.

Зависимости: httpx, selectolax (+ feedparser по желанию). Никаких импортов сервера — файл уезжает в Colab.
Реальная структура t.me/s (проверено на 2026-09): .tgme_widget_message[data-post="channel/ID"],
текст .tgme_widget_message_text, время <time datetime>, просмотры .tgme_widget_message_views,
пагинация ?before=ID, счётчик подписчиков .tgme_channel_info_counter.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

try:
    from selectolax.parser import HTMLParser
except Exception:  # pragma: no cover
    HTMLParser = None

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_count(s: str | None) -> int:
    if not s:
        return 0
    s = s.strip().upper().replace(",", ".").replace(" ", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(re.sub(r"[^\d.]", "", s) or 0) * mult)
    except ValueError:
        return 0


def _text(node) -> str:
    if node is None:
        return ""
    html = node.html or ""
    html = re.sub(r"<br\s*/?>", "\n", html)
    return HTMLParser(html).text(separator=" ").strip() if HTMLParser else re.sub(r"<[^>]+>", " ", html).strip()


# ------------------------------------------------------------------ Telegram (публичное веб-превью)
def parse_tg_page(html: str, handle: str) -> tuple[list[dict], dict]:
    tree = HTMLParser(html)
    info = {"title": "", "subscribers": 0}
    t = tree.css_first(".tgme_channel_info_header_title")
    info["title"] = t.text(strip=True) if t else handle
    for c in tree.css(".tgme_channel_info_counter"):
        txt = c.text(strip=True).lower()
        if "subscriber" in txt or "подписчик" in txt:
            info["subscribers"] = parse_count(c.css_first(".counter_value").text() if c.css_first(".counter_value") else txt)
    items = []
    for msg in tree.css(".tgme_widget_message[data-post]"):
        post = msg.attributes.get("data-post", "")
        try:
            mid = int(post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        # в ответе на пост первым идёт блок-цитата (js-message_reply_text) — свой текст поста отдельно
        texts = msg.css(".tgme_widget_message_text")
        tnode = next((n for n in texts if "js-message_reply_text" not in (n.attributes.get("class") or "")), None)
        rnode = next((n for n in texts if "js-message_reply_text" in (n.attributes.get("class") or "")), None)
        reply_a = msg.css_first("a.tgme_widget_message_reply")
        reply_href = (reply_a.attributes.get("href") or "") if reply_a else ""
        reply_post = re.sub(r"^https?://t\.me/(?:s/)?", "", reply_href).split("?")[0]
        text = _text(tnode)
        preview = msg.css_first(".tgme_widget_message_link_preview")
        if preview:
            text = (text + "\n" + _text(preview)).strip()
        tm = msg.css_first("time[datetime]")
        published = tm.attributes.get("datetime") if tm else None
        try:
            published = _iso(datetime.fromisoformat(published)) if published else None
        except ValueError:
            published = None
        views = msg.css_first(".tgme_widget_message_views")
        fwd = msg.css_first(".tgme_widget_message_forwarded_from_name")
        imgs = [re.search(r"url\('([^']+)'\)", n.attributes.get("style", "") or "") for n in
                msg.css(".tgme_widget_message_photo_wrap")]
        items.append({
            "platform": "tg", "external_id": f"tg:{post}", "url": f"https://t.me/{post}", "msg_id": mid,
            "author_id": f"tg:{handle}", "author_name": info["title"], "author_followers": info["subscribers"],
            "text": text, "published_at": published, "views": parse_count(views.text() if views else "0"),
            "forwarded_from": fwd.text(strip=True) if fwd else "",
            "media": [m.group(1) for m in imgs if m][:3],
            **({"parent_text": _text(rnode), "parent_external_id": f"tg:{reply_post}" if reply_post else None}
               if rnode is not None else {}),
        })
    return items, info


async def fetch_tg_channel(client: httpx.AsyncClient, handle: str, since_id: int = 0, max_pages: int = 3,
                           recheck: int = 0) -> tuple[list[dict], dict]:
    """Новые посты после since_id. recheck>0 — ещё раз берём последние N уже виденных: каналы дописывают посты
    («Массовые отключения…» — список адресов рос час), а просмотры растут; правки и цифры так не теряются."""
    handle = handle.lstrip("@").strip("/")
    url = f"https://t.me/s/{handle}"
    out, info = [], {}
    floor = max(0, since_id - recheck) if since_id else 0
    # X-Radar-*: подсказки нашему релею (ужать страницу до нужных постов); сам t.me их игнорирует
    hdr = {"User-Agent": UA, "X-Radar-After": str(floor), "X-Radar-Limit": "10"}
    for _ in range(max_pages):
        r = await client.get(url, headers=hdr)
        r.raise_for_status()
        items, info = parse_tg_page(r.text, handle)
        info["relay"] = r.headers.get("x-radar-slim", "")
        if not items:
            break
        out.extend(i for i in items if i["msg_id"] > floor)
        min_id = min(i["msg_id"] for i in items)
        if min_id <= floor + 1 or since_id == 0:
            break
        url = f"https://t.me/s/{handle}?before={min_id}"
    return out, info


async def fetch_tg_post(client: httpx.AsyncClient, handle: str, msg_id: int) -> dict | None:
    """Один пост канала (свежий текст и просмотры) — для динамики и перечитывания правок. Страница крошечная:
    релей отдаёт только этот пост."""
    handle = handle.lstrip("@").strip("/")
    r = await client.get(f"https://t.me/s/{handle}?before={msg_id + 1}",
                         headers={"User-Agent": UA, "X-Radar-After": str(msg_id - 1), "X-Radar-Limit": "1"})
    r.raise_for_status()
    items, _ = parse_tg_page(r.text, handle)
    return next((i for i in items if i["msg_id"] == msg_id), None)


# ------------------------------------------------------------------ RSS / Atom
def _date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return _iso(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        try:
            return _iso(datetime.fromisoformat(s.replace("Z", "+00:00")))
        except ValueError:
            return None


def parse_feed(content: bytes, feed_key: str = "") -> list[dict]:
    try:
        import feedparser  # type: ignore

        fp = feedparser.parse(content)
        out = []
        for e in fp.entries:
            pub = None
            if getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None):
                t = e.get("published_parsed") or e.get("updated_parsed")
                pub = _iso(datetime(*t[:6], tzinfo=timezone.utc))
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "")
            src = e.get("source") if isinstance(e.get("source"), dict) else {}
            out.append({"guid": e.get("id") or e.get("link"), "url": e.get("link"), "title": e.get("title", ""),
                        "text": summary.strip(), "published_at": pub,
                        "author_name": src.get("title") if src else e.get("author"),
                        "source_url": src.get("href") or src.get("url") or ""})
        return out
    except ImportError:
        pass
    root = ET.fromstring(content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        src = it.find("source")
        out.append({"guid": g("guid") or g("link"), "url": g("link"), "title": g("title"),
                    "text": re.sub(r"<[^>]+>", " ", g("description")).strip(), "published_at": _date(g("pubDate")),
                    "author_name": g("source") or g("author"), "source_url": src.attrib.get("url", "") if src is not None else ""})
    for e in root.findall("a:entry", ns):
        link = e.find("a:link", ns)
        out.append({"guid": e.findtext("a:id", "", ns), "url": link.attrib.get("href") if link is not None else "",
                    "title": e.findtext("a:title", "", ns), "text": re.sub(r"<[^>]+>", " ", e.findtext("a:summary", "", ns)),
                    "published_at": _date(e.findtext("a:updated", "", ns)), "author_name": ""})
    return out


def feed_to_items(entries: list[dict], source_key: str, platform: str = "rss") -> list[dict]:
    items = []
    for e in entries:
        guid = e.get("guid") or e.get("url")
        if not guid:
            continue
        items.append({"platform": platform, "external_id": f"{platform}:{guid}"[:500], "url": e.get("url"),
                      "source_url": e.get("source_url", ""),
                      "title": e.get("title", ""), "text": e.get("text", ""), "published_at": e.get("published_at"),
                      "author_id": f"{platform}:{source_key}", "author_name": e.get("author_name") or source_key})
    return items


def gnews_url(query: str, window: str = "1d") -> str:
    q = urllib.parse.quote(f"{query} when:{window}")
    return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"


RSS_PATHS = ["/rss", "/rss.xml", "/rss/", "/feed", "/feed/", "/rss/all", "/news/rss", "/export/rss2/index.xml",
             "/rss-feeds/rss.xml", "/yandex/rss", "/rss/news"]


async def discover_rss(client: httpx.AsyncClient, homepage: str) -> str | None:
    """Автопоиск RSS: <link rel=alternate> на главной, затем типовые пути."""
    try:
        r = await client.get(homepage, headers={"User-Agent": UA})
        if r.status_code == 200 and HTMLParser:
            for ln in HTMLParser(r.text).css("link[rel=alternate]"):
                typ = (ln.attributes.get("type") or "").lower()
                if "rss" in typ or "atom" in typ:
                    return urllib.parse.urljoin(homepage, ln.attributes.get("href") or "")
    except httpx.HTTPError:
        pass
    for p in RSS_PATHS:
        url = urllib.parse.urljoin(homepage, p)
        try:
            r = await client.get(url, headers={"User-Agent": UA})
            if r.status_code == 200 and (b"<rss" in r.content[:600] or b"<feed" in r.content[:600]):
                return url
        except httpx.HTTPError:
            continue
    return None


def extract_article(html: str) -> str:
    """Грубое извлечение текста статьи: <article>/<main>, иначе все <p>."""
    if not HTMLParser:
        return re.sub(r"<[^>]+>", " ", html)[:20000]
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "header", "footer", "aside", "form"):
        for n in tree.css(sel):
            n.decompose()
    root = tree.css_first("article") or tree.css_first("main") or tree.body
    if root is None:
        return ""
    ps = [p.text(strip=True) for p in root.css("p") if len(p.text(strip=True)) > 40]
    return ("\n".join(ps) if ps else root.text(separator=" ", strip=True))[:20000]
