"""Мост сервер ⇄ Colab. Colab только ИСХОДЯЩИМИ запросами ходит на сервер (никаких туннелей в Colab —
это и надёжнее, и не нарушает правила Colab про прокси/удалённый доступ).

  POST /api/brain/heartbeat   — «я жив»: GPU, модель, остаток CU; в ответ — конфиг и данные ретранслятора
  POST /api/brain/lease       — выдать пачку задач (пакеты с контекстом и памятью)
  POST /api/brain/result      — вернуть анализ
  POST /api/brain/tool        — инструменты агента (поиск по памяти, история автора, тред, карточка объекта)
  GET  /api/brain/code        — актуальный core.py + portable.py (одна логика анализа на сервере и в Colab)
  GET  /api/brain/collect     — план сбора для «зарубежных» источников (Telegram, Google News, …)
  POST /api/brain/ingest      — собранные в Colab элементы
  POST /api/brain/relay/lease — алерты, которые сервер не смог доставить в Telegram
  POST /api/brain/relay/ack
  POST /api/brain/jobs, /api/brain/job_result — задания на тексты отчётов
  GET  /health                — публичная проверка живости (для внешнего мониторинга)
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Header, HTTPException, Request

from ..util import ago, iso, jdump, jload

if TYPE_CHECKING:
    from ..context import Ctx

HERE = Path(__file__).resolve().parent


def make_router(ctx: "Ctx") -> APIRouter:
    r = APIRouter()

    def auth(authorization: str | None):
        tok = ctx.settings.bridge_token
        if not tok:
            raise HTTPException(403, "BRIDGE_TOKEN не задан на сервере")
        got = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got, tok):
            raise HTTPException(401, "bad token")

    @r.get("/health")
    async def health():
        st = ctx.db.stats()
        last = st["last_run"] or {}
        return {"ok": True, "last_run": last.get("finished_at"), "queue": st["queue"],
                "colab": ctx.brain.colab_online, "local_llm": ctx.brain.local_online, "ts": iso()}

    @r.post("/api/brain/heartbeat")
    async def heartbeat(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        wid = str(body.get("worker_id", "colab"))[:64]
        ctx.db.x("INSERT INTO brain_workers(worker_id, last_seen, info_json) VALUES(?,?,?) ON CONFLICT(worker_id) "
                 "DO UPDATE SET last_seen=excluded.last_seen, info_json=excluded.info_json", (wid, iso(), jdump(body)))
        ctx.brain.colab_online, ctx.brain.colab_info = True, body
        relay = {}
        if body.get("want_relay") and os.environ.get("RELAY_VIA_COLAB", "1") == "1":
            relay = {"bot_token": ctx.settings.tg_bot_token, "admins": ctx.alerts.tg_recipients(level=1)}
        return {"ok": True, "queue": ctx.db.queue_stats(), "max_rounds": ctx.settings.brain.agent_max_rounds,
                "company": ctx.settings.company, "relay": relay, "server_time": iso()}

    @r.post("/api/brain/lease")
    async def lease(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        wid = str(body.get("worker_id", "colab"))[:64]
        items = ctx.db.lease(wid, min(int(body.get("limit", 8)), 32), ctx.settings.brain.lease_sec)
        catalog = ctx.reg.catalog_text()
        pkgs = []
        for it in items:
            src = ctx.db.one("SELECT * FROM sources WHERE key=?", (it["source_key"],))
            pkgs.append(ctx.memory.package(it, catalog, dict(src) if src else None))
        return {"packages": pkgs}

    @r.post("/api/brain/result")
    async def result(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        wid = str(body.get("worker_id", "colab"))[:64]
        done = 0
        for res in body.get("results", []):
            iid = int(res.get("id", 0))
            row = ctx.db.one("SELECT status, lease_owner FROM items WHERE id=?", (iid,))
            if not row:
                continue
            if res.get("ok") and isinstance(res.get("analysis"), dict):
                ctx.pipeline.finalize(iid, res["analysis"], "colab")
                done += 1
            elif row["status"] == "leased" and row["lease_owner"] == wid:
                ctx.db.x("UPDATE items SET status='queued', lease_owner=NULL WHERE id=?", (iid,))
        return {"ok": True, "finalized": done}

    @r.post("/api/brain/tool")
    async def tool(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        item = ctx.db.item(int(body.get("item_id", 0))) or {}
        return {"result": await ctx.memory.tool(body.get("name", ""), body.get("query", ""), item)}

    @r.get("/api/brain/code")
    async def code(authorization: str | None = Header(None)):
        auth(authorization)
        return {"core": (HERE / "core.py").read_text("utf-8"),
                "portable": (HERE.parent / "collectors" / "portable.py").read_text("utf-8"),
                "worker": (HERE.parent.parent / "colab" / "brain_worker.py").read_text("utf-8")}

    @r.get("/api/brain/collect")
    async def collect_plan(authorization: str | None = Header(None)):
        auth(authorization)
        out = []
        for s in ctx.db.sources(where="colab"):
            if s.get("breaker_until") and s["breaker_until"] > iso():
                continue
            cur = ctx.db.cursor_get(f"tg:{s['handle'].lstrip('@')}", "0") if s["kind"] == "tg_channel" else None
            out.append({"key": s["key"], "kind": s["kind"], "handle": s["handle"], "tier": s["tier"],
                        "since_id": int(cur or 0), "meta": jload(s["meta_json"], {})})
        return {"sources": out, "intervals": {"hot": ctx.settings.schedule.hot_minutes,
                                              "warm": ctx.settings.schedule.warm_minutes,
                                              "cold": ctx.settings.schedule.cold_minutes}}

    @r.post("/api/brain/ingest")
    async def ingest(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        key = body.get("source_key", "")
        src = ctx.db.one("SELECT * FROM sources WHERE key=?", (key,))
        if not src:
            raise HTTPException(404, "unknown source")
        if body.get("error"):
            ctx.db.source_fail(key, "[colab] " + str(body["error"])[:300])
            return {"ok": True}
        st = ctx.pipeline.ingest(body.get("items", []), dict(src))
        ctx.db.source_ok(key, st["new"])
        for k, v in (body.get("cursors") or {}).items():
            if k.startswith(("tg:", "gn:", "rss:")):
                ctx.db.cursor_set(k, v)
        if body.get("meta_feed"):
            meta = jload(src["meta_json"], {}) or {}
            meta["feed"] = str(body["meta_feed"])[:500]
            ctx.db.x("UPDATE sources SET meta_json=? WHERE key=?", (jdump(meta), key))
        if body.get("members"):
            ctx.db.x("UPDATE sources SET members=?, verified=1 WHERE key=?", (int(body["members"]), key))
        return {"ok": True, **st}

    @r.post("/api/brain/relay/lease")
    async def relay_lease(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        return {"messages": ctx.alerts.relay_lease(str(body.get("worker_id", "colab"))[:64])}

    @r.post("/api/brain/relay/ack")
    async def relay_ack(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        ctx.alerts.relay_ack(body.get("ok", []), body.get("fail", []))
        return {"ok": True}

    @r.post("/api/brain/jobs")
    async def jobs(authorization: str | None = Header(None)):
        auth(authorization)
        jobs = ctx.db.kv_get("brain_jobs", []) or []
        pending = [j for j in jobs if j.get("status") == "pending" and j.get("created", "") >= ago(minutes=10)]
        for j in pending:
            j["status"] = "taken"
        ctx.db.kv_set("brain_jobs", jobs)
        return {"jobs": pending}

    @r.post("/api/brain/job_result")
    async def job_result(req: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await req.json()
        ctx.db.kv_set(f"job_result:{body.get('id')}", {"text": body.get("text", ""), "ts": iso()})
        return {"ok": True}

    return r
