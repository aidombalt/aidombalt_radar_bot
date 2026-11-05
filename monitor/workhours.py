"""Рабочее время PR (МСК, пн–пт, settings.schedule.work_hours) — для отчётов и «ночной» политики алертов."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .util import MSK, now_utc, parse_iso

if TYPE_CHECKING:
    from .context import Ctx


def window(ctx: "Ctx") -> tuple[int, int]:
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", ctx.settings.schedule.work_hours or "")
    return (int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])) if m else (9 * 60, 18 * 60)


def is_off(ctx: "Ctx", ts: str | datetime | None = None) -> bool:
    """Вне рабочего времени: выходные или вне окна (по умолчанию 09:00–18:00)."""
    dt = parse_iso(ts) if isinstance(ts, str) else (ts or now_utc())
    if not dt:
        return False
    loc = dt.astimezone(MSK)
    a, b = window(ctx)
    return loc.weekday() >= 5 or not (a <= loc.hour * 60 + loc.minute < b)


def next_start(ctx: "Ctx", after: datetime | None = None) -> datetime:
    """Начало ближайшего рабочего окна (если сейчас рабочее время — сейчас)."""
    now = (after or now_utc()).astimezone(MSK)
    a, _ = window(ctx)
    for d in range(0, 8):
        day = (now + timedelta(days=d)).replace(hour=a // 60, minute=a % 60, second=0, microsecond=0)
        if day.weekday() >= 5:
            continue
        if d == 0 and not is_off(ctx, now):
            return now
        if day > now:
            return day
    return now
