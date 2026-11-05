"""Ночное обслуживание: шум чистится, значимое хранится полностью, копии базы сжаты и не копятся."""
import gzip
import sqlite3
from datetime import timedelta

from monitor.settings import DATA_DIR
from monitor.util import iso, now_utc


def _item(ctx, eid, days, cand, prio=None, text="т" * 2000):
    t = iso(now_utc() - timedelta(days=days))
    return ctx.db.x("INSERT INTO items(platform, external_id, text, match_json, collected_at, published_at, candidate, "
                    "priority, relevant) VALUES('rss',?,?,'{}',?,?,?,?,?)", (eid, text, t, t, cand, prio, 1 if prio else 0))


def test_compact_keeps_what_matters(ctx):
    noise_old, noise_ancient = _item(ctx, "n1", 5, 0), _item(ctx, "n2", 20, 0)
    fresh, ours = _item(ctx, "n3", 1, 0), _item(ctx, "o1", 40, 1, "P2")
    st = ctx.db.compact(vacuum=True)
    assert st["deleted"] == 1 and st["stripped"] == 1 and st.get("vacuum")
    get = lambda i: ctx.db.one("SELECT text, match_json FROM items WHERE id=?", (i,))  # noqa: E731
    assert len(get(noise_old)["text"]) == 280 and get(noise_old)["match_json"] is None
    assert get(noise_ancient) is None
    assert len(get(fresh)["text"]) == 2000 and len(get(ours)["text"]) == 2000      # свежее и значимое — целиком


def test_backup_is_compressed_and_rotated(ctx):
    bdir = DATA_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "radar-20260920-0030.sqlite3").write_bytes(b"x" * 1000)               # несжатая копия старой версии
    for k in range(9):
        (bdir / f"radar-2026091{k}-0030.sqlite3.gz").write_bytes(b"")
    p = ctx.db.backup()
    assert p.suffix == ".gz" and not list(bdir.glob("radar-*.sqlite3"))
    assert len(list(bdir.glob("radar-*.sqlite3.gz"))) == 7
    raw = gzip.decompress(p.read_bytes())
    tmp = bdir / "check.sqlite3"
    tmp.write_bytes(raw)
    assert sqlite3.connect(tmp).execute("SELECT COUNT(*) FROM sources").fetchone()[0] > 0      # копия читается
    tmp.unlink()
