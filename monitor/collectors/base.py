"""Базовый сборщик: изоляция источников, таймауты, предохранитель (circuit breaker), учёт здоровья."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..util import iso, jload

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger("radar.collect")


class SourceError(Exception):
    pass


class SourcePaused(Exception):
    """Площадка временно ограничила запросы (Google News 503/429): пропускаем без отметки о сбое."""


class BaseCollector:
    kind = "base"
    timeout = 90.0
    parallel = 4

    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    async def fetch(self, source: dict) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    async def run(self, sources: list[dict]) -> dict:
        stats = {"sources": 0, "ok": 0, "err": 0, "new": 0, "cand": 0, "urgent": 0, "errors": []}
        sem = asyncio.Semaphore(self.parallel)

        async def one(src: dict):
            if src.get("breaker_until") and src["breaker_until"] > iso():
                return
            src = {**src, "meta": jload(src.get("meta_json"), {}) or {}}
            async with sem:
                stats["sources"] += 1
                try:
                    items = await asyncio.wait_for(self.fetch(src), timeout=self.timeout)
                    st = self.ctx.pipeline.ingest(items, src) if items else {"new": 0, "cand": 0, "urgent": 0}
                    self.ctx.db.source_ok(src["key"], st["new"])
                    stats["ok"] += 1
                    for k in ("new", "cand", "urgent"):
                        stats[k] += st.get(k, 0)
                except SourcePaused as e:   # площадка попросила подождать — это не поломка источника
                    stats["paused"] = stats.get("paused", 0) + 1
                    log.debug("источник %s на паузе: %s", src["key"], e)
                except Exception as e:  # изоляция: падение одного источника не валит цикл
                    msg = f"{type(e).__name__}: {e}"[:300]
                    self.ctx.db.source_fail(src["key"], msg)
                    stats["err"] += 1
                    stats["errors"].append(f"{src['key']}: {msg}")
                    log.warning("источник %s: %s", src["key"], msg)

        await asyncio.gather(*(one(s) for s in sources))
        return stats

    def save_meta(self, key: str, meta: dict) -> None:
        from ..util import jdump

        self.ctx.db.x("UPDATE sources SET meta_json=? WHERE key=?", (jdump(meta), key))
