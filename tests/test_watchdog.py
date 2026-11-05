"""Сторож: перерыв в работе (сон/выключение ноутбука, перезапуск) ≠ зависание сбора."""
import asyncio
from datetime import timedelta

from monitor.settings import DATA_DIR
from monitor.util import iso, now_utc


def _svc(ctx):
    from monitor.service import Service
    return Service(ctx)


def _notices(ctx, kind):
    return ctx.db.q("SELECT payload_json FROM outbox WHERE dedup_key LIKE ?", (f"adm:{kind}%",))


def _hot_run(ctx, minutes_ago):
    t = iso(now_utc() - timedelta(minutes=minutes_ago))
    ctx.db.x("INSERT INTO runs(tier, started_at, finished_at) VALUES('hot', ?, ?)", (t, t))


def test_long_downtime_is_reported_calmly(ctx):
    svc = _svc(ctx)
    (DATA_DIR / "heartbeat").write_text(iso(now_utc() - timedelta(hours=2)))   # ноутбук спал 2 часа
    _hot_run(ctx, 125)
    asyncio.run(svc.watchdog())
    assert _notices(ctx, "downtime") and "не работал 2 ч" in _notices(ctx, "downtime")[0]["payload_json"]
    assert not _notices(ctx, "hotstale")                                         # это не «завис»
    from monitor.reports import builder
    d = builder.gather(ctx, iso(now_utc() - timedelta(hours=3)), iso())
    assert any("не работал" in n for n in d["coverage_notes"])


def test_short_restart_is_silent(ctx):
    svc = _svc(ctx)
    (DATA_DIR / "heartbeat").write_text(iso(now_utc() - timedelta(minutes=15)))  # перезапуск ради обновления
    _hot_run(ctx, 16)
    asyncio.run(svc.watchdog())
    assert not _notices(ctx, "downtime") and not _notices(ctx, "hotstale")


def test_real_hang_is_reported(ctx):
    svc = _svc(ctx)
    svc._started = svc._resumed_at = now_utc() - timedelta(hours=1)             # Радар работает час
    (DATA_DIR / "heartbeat").write_text(iso(now_utc() - timedelta(minutes=1)))   # сердце бьётся
    _hot_run(ctx, 45)                                                            # а сбор встал 45 мин назад
    asyncio.run(svc.watchdog())
    n = _notices(ctx, "hotstale")
    assert n and "Сбор завис" in n[0]["payload_json"]
