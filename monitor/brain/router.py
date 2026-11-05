"""Маршрутизатор мозга: Colab (большая модель) → локальная модель на сервере → правила.

Colab Pro = 100 CU/мес ≈ 58 ч L4 / 84 ч T4 / 18 ч A100 — это НЕ 24/7. Поэтому:
  • пока Colab онлайн, он забирает очередь (срочные — первыми);
  • если Colab офлайн или срочный элемент ждёт дольше p1_max_wait_sec — разбирает локальная модель;
  • если и её нет — правила (алерт уходит с пометкой «без ИИ»);
  • когда Colab вернулся — «апгрейд»: переразбирает то, что делали local/rules за последние N часов.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from ..util import ago, iso, jload, now_utc, parse_iso
from .core import LLM, analyze, narrative

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger("radar.brain")


def local_state(ctx: "Ctx") -> str:
    """Строка статуса локальной модели для бота и GUI."""
    b, lc = ctx.brain, ctx.settings.brain.local
    name = lc.model.split("/")[-1]
    if not lc.enabled:
        return "⚪️ выключена в настройках"
    if b.local_checked is None:
        return f"⏳ проверяю… ({name})"
    if b.local_online:
        return f"🟢 {name}"
    return f"🔴 {b.local_note or 'не отвечает'}"


class BrainRouter:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.local: LLM | None = None
        self.reload_local()
        self._local_down_since: float | None = None
        self._local_down_told = False
        self._leases_checked = float("-inf")
        self._booted = False
        self._was_colab_online: bool | None = None

    def reload_local(self) -> None:
        """(Пере)создать клиент локальной модели — при старте и после смены адреса/имени модели в Настройках."""
        lc = self.ctx.settings.brain.local
        self.local = LLM(lc.base_url, lc.model, kind=lc.kind, timeout=lc.timeout, name=f"local:{lc.model.split('/')[-1]}") \
            if lc.enabled else None
        self._local_ok_checked, self._local_fails = float("-inf"), 0
        self.ctx.brain.local_checked = None

    # ---------------------------------------------------------------- статус
    def colab_online(self) -> bool:
        r = self.ctx.db.one("SELECT last_seen, info_json FROM brain_workers ORDER BY last_seen DESC LIMIT 1")
        if not r:
            return False
        seen = parse_iso(r["last_seen"])
        ok = bool(seen and (now_utc() - seen).total_seconds() < self.ctx.settings.brain.colab_offline_after_sec)
        self.ctx.brain.colab_online, self.ctx.brain.colab_info = ok, jload(r["info_json"], {})
        self.ctx.brain.last_colab_seen = r["last_seen"]
        return ok

    async def local_online(self, force: bool = False) -> bool:
        """Готова ли локальная модель (Ollama отвечает и нужная модель скачана). Проверка — не чаще раза в минуту.
        Один случайный сбой не роняет статус: «офлайн» — после двух неудач подряд (или если ещё ни разу не была онлайн)."""
        if not self.local:
            self.ctx.brain.local_note = "выключена в настройках"
            return False
        loop, b = asyncio.get_running_loop(), self.ctx.brain
        if not force and loop.time() - self._local_ok_checked <= 60:
            return b.local_online
        self._local_ok_checked = loop.time()
        ok, note = await self.local.check()
        b.local_checked, b.local_note = iso(), note
        self._local_fails = 0 if ok else self._local_fails + 1
        b.local_online = ok or (b.local_online and self._local_fails < 2)
        self._notify_local(b.local_online, note, loop.time())
        return b.local_online

    def _notify_local(self, ok: bool, note: str, now: float) -> None:
        """Сообщаем, если локальная модель недоступна дольше 5 минут (ноутбук мог только загрузиться, Ollama
        стартует позже Радара), и когда она вернулась."""
        if ok:
            if self._local_down_told:
                msg = "🧠 Локальная модель снова работает — разбор идёт ИИ, а не правилами."
                self.ctx.db.event("info", "brain", msg)
                self.ctx.alerts.admin_notice(msg, f"local:1:{now_utc():%Y%m%d%H%M}")
            self._local_down_since, self._local_down_told = None, False
            return
        self._local_down_since = self._local_down_since or now
        if not self._local_down_told and now - self._local_down_since >= 300:
            self._local_down_told = True
            colab = self.ctx.brain.colab_online
            msg = (f"🧠 Локальная модель недоступна: {note or 'не отвечает'}. "
                   + ("Пока разбирает Colab." if colab else "Colab тоже офлайн — упоминания разбираются правилами, "
                      "без ИИ (алерты приходят, но грубее).")
                   + " Проверьте Ollama (значок в трее) или имя модели в Настройках → Мозг.")
            self.ctx.db.event("warning", "brain", msg)
            self.ctx.alerts.admin_notice(msg, f"local:0:{now_utc():%Y%m%d%H}")

    # ---------------------------------------------------------------- основной такт
    async def tick(self) -> int:
        ctx, db, cfg = self.ctx, self.ctx.db, self.ctx.settings.brain
        loop = asyncio.get_running_loop()
        if not self._booted:
            self._booted = True
            n = db.release_leases(owner="server")      # разбор, прерванный перезапуском Радара
            if n:
                log.info("после перезапуска вернул в очередь %s элементов, прерванных посреди разбора", n)
        if loop.time() - self._leases_checked > 60:
            self._leases_checked = loop.time()
            n = db.release_leases()                    # Colab взял и пропал — не ждём его возвращения
            if n:
                log.info("вернул в очередь %s элементов с истёкшей арендой", n)
        colab = self.colab_online()
        await self.local_online()                      # статус держим свежим и при пустой очереди
        self._notify_state(colab)
        rows = db.q("SELECT id, urgency, queued_at FROM items WHERE status='queued' ORDER BY urgency DESC, queued_at "
                    "LIMIT 25")
        due = []
        for r in rows:
            waited = (now_utc() - (parse_iso(r["queued_at"]) or now_utc())).total_seconds()
            limit = (cfg.p1_max_wait_sec if r["urgency"] else cfg.normal_max_wait_sec) if colab else 0
            if waited >= limit:
                due.append(r["id"])
        if not due:
            if colab:
                self.upgrade_pass()
            return 0
        local_ok = await self.local_online()
        done = 0
        for iid in due[: (6 if local_ok else 25)]:
            # забираем элемент себе, чтобы Colab не взял его параллельно
            n = db.x("UPDATE items SET status='leased', lease_owner='server', lease_until=? WHERE id=? AND status='queued'",
                     (iso(now_utc() + timedelta(seconds=600)), iid))
            if not n:
                continue
            item = db.item(iid)
            try:
                if local_ok:
                    src = db.one("SELECT * FROM sources WHERE key=?", (item["source_key"],))
                    pkg = ctx.memory.package(item, ctx.reg.catalog_text(), dict(src) if src else None)
                    a = await analyze(pkg, self.local, ctx.memory.tool, max_rounds=1, company=ctx.settings.company)
                    ctx.pipeline.finalize(iid, a, "local")
                else:
                    ctx.pipeline.rules_analyze(iid)
                done += 1
            except Exception as e:
                log.warning("локальный мозг #%s: %s → правила", iid, e)
                ctx.pipeline.rules_analyze(iid)
                done += 1
        return done

    def upgrade_pass(self) -> int:
        """Colab онлайн и очередь пуста — отдаём ему на переразбор то, что делали local/rules."""
        db = self.ctx.db
        if db.scalar("SELECT COUNT(*) FROM items WHERE status IN ('queued','leased')", (), 0) > 5:
            return 0
        n = db.x("UPDATE items SET status='queued', urgency=0, queued_at=? WHERE id IN (SELECT id FROM items "
                 "WHERE analysis_level IN ('local','rules') AND analyzed_at>=? AND status='analyzed' AND "
                 "(priority IN ('P1','P2','P3') OR relevant=1) ORDER BY analyzed_at DESC LIMIT 30)",
                 (iso(), ago(hours=self.ctx.settings.brain.upgrade_hours)))
        if n:
            log.info("апгрейд: %s элементов отправлено на переразбор в Colab", n)
        return n

    def _notify_state(self, colab: bool) -> None:
        if self._was_colab_online is None:
            self._was_colab_online = colab
            return
        if colab != self._was_colab_online:
            self._was_colab_online = colab
            info = self.ctx.brain.colab_info
            if colab:
                msg = f"🧠 Colab подключился: {info.get('model', '?')} на {info.get('gpu', '?')}. Очередь переходит на большой мозг."
            else:
                msg = ("🧠 Colab отключился (сессия закончилась или кончились CU). Анализ продолжает локальная модель/"
                       "правила. Перезапустите ноутбук brain_worker.ipynb, когда будет возможность.")
            self.ctx.db.event("brain", "router", msg)
            self.ctx.alerts.admin_notice(msg, f"colab:{int(colab)}:{now_utc():%Y%m%d%H%M}")

    # ---------------------------------------------------------------- тексты для отчётов
    async def write_narrative(self, facts: str, wait_colab_sec: int = 240) -> tuple[str, str]:
        """Сначала просим Colab (через очередь заданий), затем локальную модель. ('' если никто)."""
        db = self.ctx.db
        if self.colab_online():
            job_id = f"nar-{now_utc():%Y%m%d%H%M%S}"
            jobs = db.kv_get("brain_jobs", []) or []
            jobs.append({"id": job_id, "kind": "narrative", "facts": facts, "status": "pending", "created": iso()})
            db.kv_set("brain_jobs", jobs[-20:])
            for _ in range(wait_colab_sec // 5):
                await asyncio.sleep(5)
                res = db.kv_get(f"job_result:{job_id}")
                if res:
                    return res.get("text", ""), "colab"
        if await self.local_online():
            try:
                return await narrative(self.local, facts, self.ctx.settings.company), "local"
            except Exception as e:
                log.warning("локальный нарратив: %s", e)
        return "", ""
