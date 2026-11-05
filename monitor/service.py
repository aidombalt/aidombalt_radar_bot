"""Сервис: оркестрация циклов сбора, планировщик задач, сторож (watchdog), обслуживание БД.

Собственный простой планировщик вместо внешних библиотек: меньше движущихся частей, задачи не
накладываются (пока предыдущий запуск идёт — следующий пропускается), каждая задача в таймауте.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import links
from .brain.router import BrainRouter
from .collectors import portable
from .collectors.vk import VKCollector, VKSearchCollector, build_search_queries, discover_groups
from .collectors.web import GNewsCollector, RSSCollector, TGWebCollector, WebWatchCollector
from .context import Ctx
from .reports import builder
from .settings import DATA_DIR
from .util import MSK, ago, iso, make_http, msk, now_utc, parse_iso, relay_url

log = logging.getLogger("radar.service")

TIERS = {"hot": ("hot",), "warm": ("warm",), "cold": ("cold",), "all": ("hot", "warm", "cold")}


@dataclass
class Job:
    name: str
    fn: Callable[[], Awaitable]
    every: float = 0              # секунды
    at: list[str] = field(default_factory=list)   # «ЧЧ:ММ» МСК
    weekly: str = ""              # «MON 09:30»
    timeout: float = 900
    last: float = 0
    last_slot: str = ""
    running: bool = False
    last_error: str = ""
    last_ok: str = ""


class Service:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        proxy = ctx.settings.network.http_proxy_foreign
        self.vk = VKCollector(ctx)
        self.vk_search = VKSearchCollector(ctx, self.vk.api)
        self.collectors = {
            "vk_wall": self.vk, "rss": RSSCollector(ctx), "tg_channel": TGWebCollector(ctx, proxy),
            "gnews": GNewsCollector(ctx, proxy), "web": WebWatchCollector(ctx),
        }
        self.router = BrainRouter(ctx)
        self._tg_probe_at, self._tg_direct = -1e9, False
        self._started = self._resumed_at = now_utc()      # для сторожа: перерыв в работе ≠ зависание
        self.jobs: dict[str, Job] = {}
        self._stop = asyncio.Event()
        self.last_stop: dict | None = None   # последняя остановка из GUI (monitor/power.py) — для сообщения о перерыве
        ctx.scheduler = self
        ctx.max_bot.service = self
        self._build_jobs()

    # ================================================================ задачи
    async def _outbox(self) -> int:
        try:
            self.ctx.alerts.flush_digest()          # «найдено с опозданием» — одной сводкой
        except Exception:  # noqa: BLE001 — сводка не должна мешать доставке остального
            log.exception("сводка «найдено с опозданием» не собрана")
        return await self.ctx.alerts.process()

    def _build_jobs(self):
        s = self.ctx.settings.schedule
        J = self.jobs
        J["hot"] = Job("hot", lambda: self.collect("hot"), every=s.hot_minutes * 60, timeout=s.hot_minutes * 60 * 3)
        J["warm"] = Job("warm", lambda: self.collect("warm"), every=s.warm_minutes * 60, timeout=1500)
        J["cold"] = Job("cold", lambda: self.collect("cold"), every=s.cold_minutes * 60, timeout=3600)
        J["track"] = Job("track", self.refresh_tracked, every=max(300, s.hot_minutes * 60 * 2), timeout=600)
        J["router"] = Job("router", self.router.tick, every=15, timeout=900)
        J["outbox"] = Job("outbox", self._outbox, every=5, timeout=300)
        J["max_outbox"] = Job("max_outbox", self.ctx.alerts.max.process, every=5, timeout=90)
        J["escalate"] = Job("escalate", self._escalate, every=60, timeout=60)
        J["cleanup"] = Job("cleanup", self._cleanup, every=60, timeout=300)     # закрытые карточки — из чатов
        J["watchdog"] = Job("watchdog", self.watchdog, every=60, timeout=60)
        J["links"] = Job("links", lambda: links.sweep(self.ctx, self.collectors["gnews"].client,
                                                      self.collectors["rss"].client), every=600, timeout=300)
        J["doctor"] = Job("doctor", self.doctor, every=6 * 3600, timeout=1800)
        J["discovery"] = Job("discovery", lambda: discover_groups(self.ctx, self.vk.api), at=["04:10"], timeout=1800)
        J["report"] = Job("report", lambda: builder.build(self.ctx, router=self.router), at=s.reports, timeout=900)
        J["weekly"] = Job("weekly", lambda: builder.build(self.ctx, hours=168, kind="weekly", router=self.router),
                          weekly=s.weekly_report, timeout=1200)
        J["maint"] = Job("maint", self.maintenance, at=["03:30"], timeout=1800)

    async def _escalate(self):
        try:      # сверщик «вниз» (дизайн §5.4): сначала понизить копии и чужие дома — потом эскалировать оставшиеся P1
            self.ctx.pipeline.reconcile_down()
        except Exception:  # noqa: BLE001 — сверка не должна останавливать эскалации
            log.exception("сверщик не отработал")
        try:
            self.ctx.alerts.story_summary()
        except Exception:  # noqa: BLE001
            log.exception("сводка склеек не собрана")
        return self.ctx.alerts.escalate()

    async def _cleanup(self) -> int:
        from .alerts import cleanup

        return await cleanup.sweep(self.ctx)

    def _due(self, j: Job, now: float) -> bool:
        if j.every:
            return now - j.last >= j.every
        msk_now = datetime.now(MSK)
        hm = msk_now.strftime("%H:%M")
        if j.at and hm in j.at:
            slot = msk_now.strftime("%Y-%m-%d ") + hm
            return slot != j.last_slot
        if j.weekly:
            day, _, t = j.weekly.partition(" ")
            if msk_now.strftime("%a").upper()[:3] == day.upper()[:3] and hm == t:
                slot = msk_now.strftime("%Y-%m-%d ") + hm
                return slot != j.last_slot
        return False

    async def _run(self, j: Job):
        j.running = True
        j.last = time.monotonic()
        j.last_slot = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
        try:
            await asyncio.wait_for(j.fn(), timeout=j.timeout)
            j.last_ok, j.last_error = iso(), ""
        except Exception as e:
            j.last_error = f"{type(e).__name__}: {e}"[:300]
            log.exception("задача %s упала", j.name)
            self.ctx.db.event("error", f"job:{j.name}", j.last_error)
        finally:
            j.running = False

    async def run_forever(self):
        log.info("планировщик запущен: %s", ", ".join(self.jobs))
        await asyncio.sleep(2)
        while not self._stop.is_set():
            now = time.monotonic()
            for j in self.jobs.values():
                if not j.running and self._due(j, now):
                    asyncio.create_task(self._run(j))
            await asyncio.sleep(2)

    def run_now(self, name: str) -> bool:
        j = self.jobs.get(name)
        if not j or j.running:
            return False
        asyncio.create_task(self._run(j))
        return True

    def stop(self):
        self._stop.set()

    # ================================================================ сбор
    def register_virtual_sources(self) -> None:
        """Поисковые запросы ВК и Google News как «источники» — чтобы видеть их здоровье в GUI."""
        for q in build_search_queries(self.ctx):
            self.ctx.db.upsert_source({"key": f"vk_search:{q['q']}", "kind": "vk_search", "handle": q["q"],
                                       "title": f"🔎 ВК: {q['q']}", "tier": "warm", "category": "search",
                                       "where": "server", "meta": {"priority": q.get("priority", False)}})
            if q.get("priority") or len(q["q"]) > 6:
                self.ctx.db.upsert_source({"key": f"gnews:{q['q']}", "kind": "gnews", "handle": q["q"],
                                           "title": f"📰 Google News: {q['q']}", "tier": "warm", "category": "media",
                                           "where": "colab"})

    async def collect(self, tier: str) -> dict:
        db = self.ctx.db
        rid = db.x("INSERT INTO runs(tier, started_at) VALUES(?,?)", (tier, iso()))
        total = {"new": 0, "cand": 0, "urgent": 0, "err": 0, "sources": 0}
        tiers = TIERS.get(tier, (tier,))
        srcs = [s for s in db.sources(where="server") if s["tier"] in tiers]
        srcs += await self._foreign_fallback(tiers)
        by_kind: dict[str, list[dict]] = {}
        for s in srcs:
            by_kind.setdefault(s["kind"], []).append(s)
        tasks = []
        for kind, lst in by_kind.items():
            if kind == "vk_search":
                continue
            coll = self.collectors.get(kind)
            if coll is None:
                continue
            if kind == "gnews":
                lst = coll.plan(lst)
                if not lst:
                    continue
            if kind == "vk_wall" and not self.vk.api.ready:
                for src in lst:
                    db.x("UPDATE sources SET last_error=?, last_error_at=? WHERE key=?",
                         ("не задан VK_SERVICE_TOKEN в .env — ВКонтакте не опрашивается", iso(), src["key"]))
                continue
            tasks.append(coll.run(lst))
        if "warm" in tiers and self.vk.api.ready:
            qs = [{"q": s["handle"], "priority": (s.get("meta_json") or "").find('"priority": true') >= 0}
                  for s in by_kind.get("vk_search", [])]
            plan = {q["q"] for q in self.vk_search.plan(qs)}
            tasks.append(self.vk_search.run([s for s in by_kind.get("vk_search", []) if s["handle"] in plan]))
        for st in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(st, Exception):
                total["err"] += 1
                log.exception("сборщик упал", exc_info=st)
                continue
            for k in ("new", "cand", "urgent", "sources"):
                total[k] += st.get(k, 0)
            total["err"] += st.get("err", 0)
        db.x("UPDATE runs SET finished_at=?, n_new=?, n_cand=?, n_err=?, note=? WHERE id=?",
             (iso(), total["new"], total["cand"], total["err"], f"источников {total['sources']}", rid))
        log.info("цикл %s: источников %s, новых %s, кандидатов %s (срочных %s), ошибок %s", tier, total["sources"],
                 total["new"], total["cand"], total["urgent"], total["err"])
        if tier == "hot" and os.environ.get("HEALTHCHECK_URL"):
            asyncio.create_task(self._ping_healthcheck())
        if total["urgent"]:
            asyncio.create_task(self.router.tick())   # не ждём такта — срочное сразу в работу
        return total

    async def _foreign_fallback(self, tiers) -> list[dict]:
        """Colab офлайн → Telegram-каналы (и Google News, если задан зарубежный прокси) опрашивает сам сервер,
        если t.me отсюда открывается. Проверка раз в 30 мин, чтобы не жечь предохранители источников впустую."""
        b = self.ctx.brain
        if self.router.colab_online():
            b.foreign_mode = "colab"
            return []
        kinds = {"tg_channel"} | ({"gnews"} if self.ctx.settings.network.http_proxy_foreign or relay_url() else set())
        srcs = [s for s in self.ctx.db.sources(where="colab") if s["tier"] in tiers and s["kind"] in kinds]
        if not srcs:
            return []
        loop = asyncio.get_running_loop()
        if loop.time() - self._tg_probe_at > 1800:
            self._tg_probe_at = loop.time()
            try:
                r = await self.collectors["tg_channel"].client.get("https://t.me/s/telegram", timeout=15)
                self._tg_direct = r.status_code == 200 and "tgme_widget_message" in r.text
            except Exception as e:
                log.debug("t.me probe: %s", e)
                self._tg_direct = False
            log.log(logging.INFO if self._tg_direct else logging.WARNING,
                    "Colab офлайн: Telegram-каналы %s", ("опрашивает сервер через релей" if relay_url() else
                                                           "опрашивает сервер напрямую") if self._tg_direct
                    else "НЕ опрашиваются — t.me с сервера не открывается (нужен Colab или зарубежный прокси)")
        b.foreign_mode = "server" if self._tg_direct else "none"
        return srcs if self._tg_direct else []

    async def refresh_item_metrics(self, iid: int) -> bool:
        """Свежие цифры (и текст — посты дописывают) одной публикации: для «📈 Динамика» и слежения за значимыми."""
        db = self.ctx.db
        it = db.item(iid)
        if not it:
            return False
        if it["platform"] == "tg" and "tg_channel" in self.collectors:
            handle, _, mid = it["external_id"].removeprefix("tg:").rpartition("/")
            post = await portable.fetch_tg_post(self.collectors["tg_channel"].client, handle, int(mid))
            if not post:
                return False
            if post.get("forwarded_from"):
                post["text"] = f"[переслано из: {post['forwarded_from']}] {post['text']}"
            src = db.one("SELECT * FROM sources WHERE key=?", (it["source_key"],))
            self.ctx.pipeline.ingest([post], dict(src) if src else {"key": it["source_key"], "category": "public"})
        elif it["platform"] == "vk_post" and self.vk.api.ready:
            resp = await self.vk.api.call("wall.getById", {"posts": it["external_id"].removeprefix("vk:")})
            items = resp.get("items", resp) if isinstance(resp, dict) else resp
            if not items:
                return False
            p = items[0]
            db.x("UPDATE items SET likes=?, comments=?, reposts=?, views=? WHERE id=?",
                 ((p.get("likes") or {}).get("count", 0), (p.get("comments") or {}).get("count", 0),
                  (p.get("reposts") or {}).get("count", 0), (p.get("views") or {}).get("count", 0), iid))
        else:
            return False
        it = db.item(iid)
        if not db.one("SELECT 1 FROM metrics WHERE item_id=? AND ts>?", (iid, ago(minutes=1))):
            db.x("INSERT INTO metrics(item_id, ts, likes, comments, reposts, views) VALUES(?,?,?,?,?,?)",
                 (iid, iso(), it["likes"] or 0, it["comments"] or 0, it["reposts"] or 0, it["views"] or 0))
        return True

    async def refresh_tracked(self) -> int:
        """Динамика значимых постов: Telegram (t.me отдаёт только ленту — посты перечитываем по одному) и ВК,
        найденные поиском (у них нет «родной» стены в опросе)."""
        n_tg = 0
        if "tg_channel" in self.collectors and (relay_url() or self._tg_direct):
            for r in self.ctx.db.q(
                    "SELECT i.id FROM items i WHERE i.platform='tg' AND i.track_until>? AND i.deleted=0 AND i.priority "
                    "IN ('P1','P2','P3') AND NOT EXISTS (SELECT 1 FROM metrics m WHERE m.item_id=i.id AND m.ts>?) "
                    "ORDER BY i.priority, i.published_at DESC LIMIT 30", (iso(), ago(minutes=25))):
                try:
                    n_tg += await self.refresh_item_metrics(r["id"])
                except Exception as e:  # noqa: BLE001 — один пост не должен ронять слежение за остальными
                    log.debug("динамика #%s: %s", r["id"], e)
                await asyncio.sleep(0.5)
        if not self.vk.api.ready:
            return n_tg
        rows = self.ctx.db.q("SELECT id, external_id FROM items WHERE platform='vk_post' AND track_until>? "
                             "AND source_key LIKE 'vk_search:%' LIMIT 100", (iso(),))
        if not rows:
            return n_tg
        posts = ",".join(r["external_id"].removeprefix("vk:") for r in rows)
        resp = await self.vk.api.call("wall.getById", {"posts": posts})
        items = resp.get("items", resp) if isinstance(resp, dict) else resp
        byid = {f"vk:{p['owner_id']}_{p['id']}": p for p in items or []}
        n = 0
        for r in rows:
            p = byid.get(r["external_id"])
            if not p:
                self.ctx.db.x("UPDATE items SET deleted=1 WHERE id=? AND deleted=0", (r["id"],))
                continue
            vals = ((p.get("likes") or {}).get("count", 0), (p.get("comments") or {}).get("count", 0),
                    (p.get("reposts") or {}).get("count", 0), (p.get("views") or {}).get("count", 0))
            self.ctx.db.x("UPDATE items SET likes=?, comments=?, reposts=?, views=? WHERE id=?", (*vals, r["id"]))
            self.ctx.db.x("INSERT INTO metrics(item_id, ts, likes, comments, reposts, views) VALUES(?,?,?,?,?,?)",
                          (r["id"], iso(), *vals))
            n += 1
        # «разгоняется»: переоценить риск у тех, чья динамика выросла
        for r in rows:
            it = self.ctx.db.item(r["id"])
            if it and it["status"] == "analyzed" and it["analysis_json"]:
                from .util import jload

                a = jload(it["analysis_json"], {})
                a.pop("_risk", None)
                self.ctx.pipeline.finalize(r["id"], a, it["analysis_level"] or "rules")
        return n + n_tg

    # ================================================================ сторож
    async def _ping_healthcheck(self):
        """Внешний «сторож мёртвой руки» (healthchecks.io и т.п.): Радар отмечается после каждого горячего цикла;
        если отметки нет — сервис сам пишет вам. Только так можно узнать, что ноутбук выключен, в моменте."""
        try:
            async with make_http(timeout=15) as c:          # через релей, если hc-ping.com закрыт в сети офиса
                await c.get(os.environ["HEALTHCHECK_URL"].strip())
        except Exception as e:  # noqa: BLE001
            log.debug("healthcheck: %s", e)

    async def watchdog(self):
        """Сторож (раз в минуту). Различает две разные вещи:
        1) Радар НЕ РАБОТАЛ — ноутбук спал/выключался или Радар перезапускали. Изнутри это видно только по возвращении
           (по «сердцебиению» data/heartbeat): сообщаем спокойно и один раз, если перерыв ≥ часа — алерты за это
           время пришли с опозданием, пропущенное дочитывается. Короткие перезапуски (обновления) — молча, в журнал.
        2) Радар РАБОТАЕТ, а «горячий» сбор не завершается — настоящее зависание: тогда ⛔️ с подробностями."""
        from .reports.situation import dur

        db, s = self.ctx.db, self.ctx.settings
        now = now_utc()
        hb = DATA_DIR / "heartbeat"
        try:
            prev = parse_iso(hb.read_text().strip()) if hb.exists() else None
        except (OSError, ValueError):
            prev = None
        hb.write_text(iso(now))
        grace = timedelta(minutes=s.schedule.hot_minutes * 4)
        if prev and now - prev > timedelta(minutes=10):
            gap = now - prev
            self._resumed_at = now
            downs = (db.kv_get("downtimes", []) or [])[-49:] + [{"from": iso(prev), "to": iso(now)}]
            db.kv_set("downtimes", downs)
            db.event("info", "watchdog", f"перерыв в работе {dur(gap)}: {msk(prev)} — {msk(now)}")
            log.warning("Радар не работал %s (%s — %s): сон/выключение ноутбука или перезапуск", dur(gap), msk(prev), msk(now))
            stop, self.last_stop = self.last_stop, None
            if gap >= timedelta(minutes=s.alerts.downtime_notice_min):
                why = (f"его выключили из веб-интерфейса ({stop.get('by') or 'GUI'}, {msk(stop.get('at'))})"
                       if stop and stop.get("action") == "stop" else
                       "ноутбук спал или был выключен, либо Радар перезапускали")
                self.ctx.alerts.admin_notice(
                    f"ℹ️ Радар не работал {dur(gap)} ({msk(prev)} — {msk(now)}): {why}. Алертов за это время не было; "
                    "пропущенное сейчас дочитывается — если было срочное, алерт придёт в ближайшие минуты.",
                    f"downtime:{prev:%Y%m%d%H%M}")
        # настоящее зависание: Радар жив (сердцебиение свежее) дольше «запаса», а горячий сбор так и не завершился
        alive_for = now - max(self._started, self._resumed_at)
        last_hot = db.one("SELECT finished_at FROM runs WHERE tier='hot' AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        age = now - parse_iso(last_hot["finished_at"]) if last_hot else alive_for
        if alive_for > grace and age > grace:
            j = self.jobs.get("hot")
            detail = (f"цикл идёт уже {dur(timedelta(seconds=time.monotonic() - j.last))}" if j and j.running else
                      f"последняя ошибка: {j.last_error}" if j and j.last_error else "")
            self.ctx.alerts.admin_notice(
                f"⛔️ Сбор завис: Радар работает, но «горячий» цикл не завершался {dur(age)}"
                + (f" ({detail})" if detail else "") + ". Обычно помогает перезапуск (закрыть окно Радара и снова "
                "запустить start_radar.bat). Если повторится — пришлите data/radar.log.", f"hotstale:{now:%Y%m%d%H}")
        hot = db.q("SELECT consecutive_failures FROM sources WHERE enabled=1 AND candidate=0 AND tier='hot' AND where_='server'")
        bad = sum(1 for r in hot if (r["consecutive_failures"] or 0) >= 3)
        if hot and bad / len(hot) > 0.5:
            self.ctx.alerts.admin_notice(f"⚠️ Не работают {bad} из {len(hot)} «горячих» источников (губернаторы, надзор). "
                                         f"Возможна проблема с токеном VK или сетью.", f"hotbad:{now:%Y%m%d%H}")
        tok = db.one("SELECT message FROM events WHERE component='vk' AND level='error' AND ts>=? ORDER BY id DESC LIMIT 1",
                     (ago(hours=1),))
        if tok:
            self.ctx.alerts.admin_notice(f"🔑 {tok['message']}", f"vktoken:{now:%Y%m%d}")
        stuck = db.scalar("SELECT COUNT(*) FROM outbox WHERE status IN ('pending','relay') AND created_at<?", (ago(minutes=30),), 0)
        if stuck and (db.kv_get("stuck_note_at") or "") < ago(minutes=30):
            db.kv_set("stuck_note_at", iso())
            db.event("warn", "watchdog", f"недоставленных сообщений старше 30 мин: {stuck}")

    async def doctor(self):
        """«Доктор источников»: разрешить адреса ВК, найти RSS, пометить протухшие (нет постов 30+ дней)."""
        db = self.ctx.db
        if getattr(self.ctx, "max_bot", None):
            await self.ctx.max_bot.check_health()
        self.ctx.fill_watch_ids()
        for s in db.sources(kind="vk_wall"):
            if not s["verified"] and self.vk.api.ready:
                try:
                    from .util import jload

                    await self.vk.resolve({**s, "meta": jload(s["meta_json"], {}) or {}})
                except Exception as e:
                    db.source_fail(s["key"], f"не удалось разрешить адрес: {e}")
        stale = db.q("SELECT key, title FROM sources WHERE enabled=1 AND candidate=0 AND last_ok IS NOT NULL "
                     "AND (last_item_at IS NULL OR last_item_at<?) AND kind IN ('vk_wall','tg_channel','rss')", (ago(days=30),))
        for r in stale:
            db.x("UPDATE sources SET note=? WHERE key=?", ("нет новых публикаций 30+ дней (площадка переехала?)", r["key"]))

    async def maintenance(self):
        """Ночное обслуживание: чистка шума и сжатие базы, сжатая резервная копия, старые отчёты, дайджест."""
        db = self.ctx.db
        st = await asyncio.to_thread(db.compact)
        db.x("UPDATE incidents SET status='closed' WHERE status='open' AND last_seen<?", (ago(days=7),))
        await asyncio.to_thread(db.backup)
        old_reports = 0
        for f in (DATA_DIR / "reports").glob("*.*"):          # файлы отчётов старше 90 дней (письма уже ушли)
            if f.stat().st_mtime < time.time() - 90 * 86400:
                f.unlink(missing_ok=True)
                old_reports += 1
        await self._digest()
        db.event("info", "maint", f"обслуживание: шум без текста {st['stripped']}, удалено {st['deleted']}, в архив "
                                  f"{st['archived']}, сжатие базы {'да' if st.get('vacuum') else 'не нужно'}, "
                                  f"старых файлов отчётов {old_reports}; сжатая копия базы сделана")

    async def _digest(self):
        """Средняя память: суточная выжимка (ИИ, если доступен) → таблица digests."""
        day = (datetime.now(MSK) - timedelta(days=1)).strftime("%Y-%m-%d")
        if self.ctx.db.one("SELECT 1 FROM digests WHERE day=?", (day,)):
            return
        d = builder.gather(self.ctx, ago(hours=27), ago(hours=3))
        text, _ = await self.router.write_narrative(builder.facts_text(d), wait_colab_sec=60) if d["items"] else ("", "")
        text = text or builder.template_narrative(d)
        self.ctx.db.x("INSERT INTO digests(day, text, created_at) VALUES(?,?,?)", (day, text, iso()))
        self.ctx.memory.add_knowledge("digest", text[:1500], f"digest:{day}", approved=False, key=day)
