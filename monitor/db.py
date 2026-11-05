"""SQLite (WAL) — единственное хранилище: элементы, очередь ИИ, инциденты, память, исходящие, источники.

Почему SQLite: один файл, переживает рестарты, бэкапится одной командой, держит десятки тысяч
записей в сутки без администрирования. Все записи атомарны; очередь ИИ — на «лизах» (lease),
поэтому падение Colab ничего не теряет: истёкшая лиза возвращает элемент в очередь.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .settings import DATA_DIR
from datetime import timedelta

from .util import ago, iso, jdump, jload, now_utc

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,              -- vk_post | vk_comment | tg | rss | gnews | web | manual
  external_id TEXT NOT NULL,
  source_key TEXT,
  url TEXT,
  author_id TEXT, author_name TEXT, author_followers INTEGER DEFAULT 0,
  parent_external_id TEXT, parent_text TEXT,
  title TEXT, text TEXT,
  published_at TEXT, collected_at TEXT,
  likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, views INTEGER DEFAULT 0,
  media_json TEXT,
  content_hash TEXT,
  edited INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
  candidate INTEGER DEFAULT 0,
  match_json TEXT, match_score REAL DEFAULT 0, match_hint TEXT,
  status TEXT DEFAULT 'new',           -- new | queued | leased | analyzed | skipped
  urgency INTEGER DEFAULT 0,           -- 1 = сильный сигнал, ждать Colab недолго
  analysis_level TEXT,                 -- rules | local | colab
  analysis_json TEXT,
  relevant INTEGER, sentiment INTEGER, priority TEXT, risk_score REAL DEFAULT 0,
  tags TEXT, summary TEXT,
  incident_id INTEGER,
  lease_owner TEXT, lease_until TEXT,
  queued_at TEXT, analyzed_at TEXT,
  workflow TEXT DEFAULT 'open',        -- open | in_work | resolved | false_positive
  operator TEXT, operator_note TEXT,
  track_until TEXT,
  UNIQUE(platform, external_id)
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status, urgency, queued_at);
CREATE INDEX IF NOT EXISTS ix_items_pub ON items(published_at);
CREATE INDEX IF NOT EXISTS ix_items_prio ON items(priority, analyzed_at);
CREATE INDEX IF NOT EXISTS ix_items_track ON items(track_until);
CREATE INDEX IF NOT EXISTS ix_items_author ON items(author_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS metrics(item_id INTEGER, ts TEXT, likes INTEGER, comments INTEGER, reposts INTEGER, views INTEGER);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(item_id, ts);

CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, title TEXT, entity_key TEXT, topic TEXT,
  first_seen TEXT, last_seen TEXT, n_items INTEGER DEFAULT 0, max_priority TEXT,
  status TEXT DEFAULT 'open', summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc ON incidents(entity_key, topic, last_seen);

CREATE TABLE IF NOT EXISTS authors(
  key TEXT PRIMARY KEY, platform TEXT, name TEXT, followers INTEGER DEFAULT 0,
  n_items INTEGER DEFAULT 0, n_negative INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT,
  watch INTEGER DEFAULT 0, note TEXT
);

CREATE TABLE IF NOT EXISTS sources(
  key TEXT PRIMARY KEY, kind TEXT, handle TEXT, title TEXT, tier TEXT, region TEXT, category TEXT,
  where_ TEXT DEFAULT 'server', enabled INTEGER DEFAULT 1, comments INTEGER DEFAULT 0,
  meta_json TEXT, verified INTEGER DEFAULT 0, candidate INTEGER DEFAULT 0, members INTEGER DEFAULT 0,
  last_ok TEXT, last_error TEXT, last_error_at TEXT, consecutive_failures INTEGER DEFAULT 0,
  breaker_until TEXT, last_item_at TEXT, n_items_total INTEGER DEFAULT 0, added_at TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS cursors(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, channel TEXT, recipient TEXT, payload_json TEXT, dedup_key TEXT UNIQUE,
  status TEXT DEFAULT 'pending',       -- pending | relay | sent | dead
  attempts INTEGER DEFAULT 0, next_attempt_at TEXT, created_at TEXT, sent_at TEXT, last_error TEXT,
  item_id INTEGER, priority TEXT, relay_owner TEXT, relay_until TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox ON outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, item_id INTEGER UNIQUE, priority TEXT, created_at TEXT,
  acked_by TEXT, acked_at TEXT, escalations INTEGER DEFAULT 0, last_notified_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge(
  id INTEGER PRIMARY KEY, kind TEXT, key TEXT, text TEXT, source TEXT,
  approved INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, item_id INTEGER, user TEXT, verdict TEXT, note TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS users(tg_id TEXT PRIMARY KEY, name TEXT, role TEXT DEFAULT 'viewer', email TEXT, level INTEGER DEFAULT 1, active INTEGER DEFAULT 1, added_at TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, kind TEXT, period_from TEXT, period_to TEXT, created_at TEXT, xlsx_path TEXT, docx_path TEXT, n_items INTEGER, sent INTEGER DEFAULT 0, summary TEXT);
CREATE TABLE IF NOT EXISTS alert_msgs(item_id INTEGER, chat_id TEXT, message_id INTEGER, sent_at TEXT, PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS alert_pings(item_id INTEGER, chat_id TEXT, message_id INTEGER, sent_at TEXT, PRIMARY KEY(chat_id, message_id));
CREATE INDEX IF NOT EXISTS ix_alert_msgs_item ON alert_msgs(item_id);
CREATE TABLE IF NOT EXISTS max_msgs(
  item_id INTEGER, chat_id TEXT NOT NULL, mid TEXT NOT NULL PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'card', sent_at TEXT, editable INTEGER DEFAULT 1,
  state TEXT DEFAULT '', pinned INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_max_one_card ON max_msgs(item_id, chat_id) WHERE kind='card';
CREATE TABLE IF NOT EXISTS max_events(
  event_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, status TEXT DEFAULT 'pending',
  created_at TEXT, done_at TEXT, attempts INTEGER DEFAULT 0, next_attempt_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS action_receipts(
  channel TEXT, event_key TEXT, result_json TEXT, created_at TEXT,
  PRIMARY KEY(channel, event_key)
);
CREATE TABLE IF NOT EXISTS account_links(
  digest TEXT PRIMARY KEY, tg_id TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS card_refresh(
  item_id INTEGER PRIMARY KEY, state TEXT, note TEXT DEFAULT '', revision INTEGER DEFAULT 1,
  next_attempt_at TEXT, attempts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS brain_workers(worker_id TEXT PRIMARY KEY, last_seen TEXT, info_json TEXT);
CREATE TABLE IF NOT EXISTS digests(day TEXT PRIMARY KEY, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, tier TEXT, started_at TEXT, finished_at TEXT, n_new INTEGER, n_cand INTEGER, n_err INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, component TEXT, message TEXT);
"""


class DB:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or DATA_DIR / "radar.sqlite3")
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        with self.tx():
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(users)")}
            if "max_user_id" not in columns:
                self.conn.execute("ALTER TABLE users ADD COLUMN max_user_id TEXT")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_max ON users(max_user_id) "
                              "WHERE max_user_id IS NOT NULL AND max_user_id<>''")

    # ---------- низкоуровневое ----------
    @contextmanager
    def tx(self):
        with self._lock:
            nested = self.conn.in_transaction
            savepoint = "nested_" + uuid.uuid4().hex
            self.conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute(f"RELEASE {savepoint}" if nested else "COMMIT")
            except Exception:
                if nested:
                    self.conn.execute(f"ROLLBACK TO {savepoint}")
                    self.conn.execute(f"RELEASE {savepoint}")
                else:
                    self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        """INSERT → id новой строки (0 если проигнорирована); UPDATE/DELETE → число затронутых строк."""
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            if sql.lstrip()[:6].upper() == "INSERT":
                return cur.lastrowid if cur.rowcount > 0 else 0
            return cur.rowcount

    def scalar(self, sql: str, args: Iterable[Any] = (), default: Any = None) -> Any:
        r = self.one(sql, args)
        return r[0] if r and r[0] is not None else default

    # ---------- kv / cursors ----------
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return jload(r["value"], default) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, jdump(value)))

    def cursor_get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM cursors WHERE key=?", (key,))
        return r["value"] if r else default

    def cursor_set(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO cursors(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), iso()),
        )

    def event(self, level: str, component: str, message: str) -> None:
        self.x("INSERT INTO events(ts,level,component,message) VALUES(?,?,?,?)", (iso(), level, component, message[:2000]))

    # ---------- items ----------
    def upsert_item(self, it: dict) -> tuple[int, str]:
        """Вставка/обновление. Возвращает (id, 'new'|'edited'|'same')."""
        with self.tx() as c:
            row = c.execute(
                "SELECT id, content_hash, likes, comments, reposts, views FROM items WHERE platform=? AND external_id=?",
                (it["platform"], it["external_id"]),
            ).fetchone()
            if row is None:
                cols = [k for k in it.keys()]
                c.execute(
                    f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [it[k] for k in cols],
                )
                iid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                return iid, "new"
            iid = row["id"]
            kind = "same"
            upd = {k: it.get(k) for k in ("likes", "comments", "reposts", "views", "author_followers") if it.get(k) is not None}
            if it.get("content_hash") and it["content_hash"] != row["content_hash"]:
                upd.update(text=it.get("text"), title=it.get("title"), content_hash=it["content_hash"], edited=1)
                if it.get("parent_text"):          # Telegram: ответ на пост — цитата пригодится анализу
                    upd.update(parent_text=it["parent_text"], parent_external_id=it.get("parent_external_id"))
                kind = "edited"
            if upd:
                c.execute(f"UPDATE items SET {','.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), iid])
            return iid, kind

    def fts_put(self, item_id: int, body: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM items_fts WHERE rowid=?", (item_id,))
            self.conn.execute("INSERT INTO items_fts(rowid, body) VALUES(?,?)", (item_id, body))

    def fts_search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        toks = [t for t in query.replace('"', " ").split() if len(t) > 1][:12]
        if not toks:
            return []
        expr = " OR ".join(f'"{t}"*' for t in toks)
        return self.q(
            "SELECT i.* FROM items_fts f JOIN items i ON i.id=f.rowid WHERE items_fts MATCH ? "
            "ORDER BY bm25(items_fts) LIMIT ?",
            (expr, limit),
        )

    def item(self, iid: int) -> dict | None:
        r = self.one("SELECT * FROM items WHERE id=?", (iid,))
        return dict(r) if r else None

    # ---------- очередь ИИ (lease) ----------
    def enqueue(self, iid: int, urgency: int) -> None:
        self.x("UPDATE items SET status='queued', urgency=?, queued_at=? WHERE id=? AND status IN ('new','skipped','analyzed')",
               (urgency, iso(), iid))

    def release_leases(self, owner: str | None = None) -> int:
        """Вернуть в очередь «зависшие в работе»: с истёкшей арендой (исполнитель пропал — Colab отключился,
        процесс упал) или, если указан owner, все его аренды (Радар перезапустился посреди разбора)."""
        if owner:
            return self.x("UPDATE items SET status='queued', lease_owner=NULL, lease_until=NULL "
                          "WHERE status='leased' AND lease_owner=?", (owner,))
        return self.x("UPDATE items SET status='queued', lease_owner=NULL, lease_until=NULL "
                      "WHERE status='leased' AND (lease_until IS NULL OR lease_until < ?)", (iso(),))

    def lease(self, owner: str, limit: int, lease_sec: int, min_level: str | None = None) -> list[dict]:
        """Выдать пачку элементов исполнителю. Приоритет: срочные, затем старые."""
        until = iso(now_utc() + timedelta(seconds=lease_sec))
        with self.tx() as c:
            c.execute("UPDATE items SET status='queued', lease_owner=NULL, lease_until=NULL "
                      "WHERE status='leased' AND lease_until < ?", (iso(),))
            rows = c.execute(
                "SELECT id FROM items WHERE status='queued' ORDER BY urgency DESC, queued_at ASC LIMIT ?", (limit,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                c.execute(
                    f"UPDATE items SET status='leased', lease_owner=?, lease_until=? WHERE id IN ({','.join('?' * len(ids))})",
                    [owner, until, *ids],
                )
        return [self.item(i) for i in ids]

    def queue_stats(self) -> dict:
        r = self.one(
            "SELECT SUM(status='queued') q, SUM(status='leased') l, SUM(status='queued' AND urgency=1) u, "
            "MIN(CASE WHEN status='queued' THEN queued_at END) oldest FROM items"
        )
        return {"queued": r["q"] or 0, "leased": r["l"] or 0, "urgent": r["u"] or 0, "oldest": r["oldest"]}

    # ---------- источники ----------
    def source_ok(self, key: str, n_new: int = 0) -> None:
        self.x(
            "UPDATE sources SET last_ok=?, consecutive_failures=0, breaker_until=NULL, "
            "last_item_at=CASE WHEN ?>0 THEN ? ELSE last_item_at END, n_items_total=n_items_total+? WHERE key=?",
            (iso(), n_new, iso(), n_new, key),
        )

    def source_fail(self, key: str, err: str, breaker_after: int = 5, breaker_minutes: int = 20) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sources SET last_error=?, last_error_at=?, consecutive_failures=consecutive_failures+1 WHERE key=?",
                (err[:500], iso(), key),
            )
            r = c.execute("SELECT consecutive_failures FROM sources WHERE key=?", (key,)).fetchone()
            if r and r[0] >= breaker_after:
                mult = min(8, 2 ** ((r[0] - breaker_after) // 3))
                c.execute("UPDATE sources SET breaker_until=? WHERE key=?",
                          (iso(now_utc() + timedelta(minutes=breaker_minutes * mult)), key))

    def sources(self, kind: str | None = None, enabled_only: bool = True, where: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM sources WHERE candidate=0", []
        if enabled_only:
            sql += " AND enabled=1"
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if where:
            sql += " AND where_=?"
            args.append(where)
        return [dict(r) for r in self.q(sql + " ORDER BY tier, key", args)]

    def upsert_source(self, s: dict, keep_state: bool = True) -> None:
        cols = ["key", "kind", "handle", "title", "tier", "region", "category", "where_", "enabled", "comments",
                "meta_json", "verified", "candidate", "members", "note"]
        vals = [s.get(c if c != "where_" else "where", s.get(c)) for c in cols]
        vals[cols.index("enabled")] = int(s.get("enabled", 1))
        vals[cols.index("comments")] = int(bool(s.get("comments", 0)))
        vals[cols.index("candidate")] = int(bool(s.get("candidate", 0)))
        vals[cols.index("verified")] = int(bool(s.get("verified", 0)))
        vals[cols.index("where_")] = s.get("where", s.get("where_", "server"))
        vals[cols.index("meta_json")] = jdump(s.get("meta", {})) if not isinstance(s.get("meta_json"), str) else s["meta_json"]
        upd = ",".join(f"{c}=excluded.{c}" for c in cols[1:] if not (keep_state and c in ("verified", "members")))
        self.x(
            f"INSERT INTO sources({','.join(cols)},added_at) VALUES({','.join('?' * len(cols))},?) "
            f"ON CONFLICT(key) DO UPDATE SET {upd}",
            [*vals, iso()],
        )

    # ---------- статистика для GUI/бота ----------
    def stats(self) -> dict:
        since = ago(hours=24)
        r = self.one(
            "SELECT COUNT(*) n, SUM(candidate) c, SUM(priority='P1') p1, SUM(priority='P2') p2, SUM(priority='P3') p3 "
            "FROM items WHERE collected_at>=?",
            (since,),
        )
        # площадки (каналы, стены, сайты) и поисковые запросы считаем раздельно: 74 запроса Google News на паузе ≠ 74 поломки
        src = self.one(
            "SELECT COUNT(*) n, SUM(consecutive_failures>=3) bad, SUM(breaker_until>?) open_ FROM sources "
            "WHERE enabled=1 AND candidate=0 AND kind NOT IN ('vk_search','gnews','manual')", (iso(),))
        qs = self.one("SELECT SUM(kind='vk_search') vk, SUM(kind='gnews') gn FROM sources WHERE enabled=1 AND candidate=0")
        last = self.one("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        return {
            "items_24h": r["n"] or 0, "cand_24h": r["c"] or 0,
            "p1_24h": r["p1"] or 0, "p2_24h": r["p2"] or 0, "p3_24h": r["p3"] or 0,
            "sources": src["n"] or 0, "sources_bad": src["bad"] or 0, "sources_open": src["open_"] or 0,
            "queries_vk": qs["vk"] or 0, "queries_gnews": qs["gn"] or 0,
            "last_run": dict(last) if last else None, "queue": self.queue_stats(),
        }

    def backup(self, keep: int = 7) -> Path:
        """Сжатая копия базы: VACUUM INTO (без пустот) + gzip — в 5–8 раз меньше. Хранится `keep` последних.
        Восстановить: распаковать .gz (7-Zip/WinRAR) и положить вместо data/radar.sqlite3 (удалив -wal и -shm) при остановленном Радаре."""
        bdir = DATA_DIR / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = f"{now_utc():%Y%m%d-%H%M}"
        tmp, dst = bdir / f"radar-{stamp}.tmp", bdir / f"radar-{stamp}.sqlite3.gz"
        tmp.unlink(missing_ok=True)
        with self._lock:
            self.conn.execute("VACUUM INTO ?", (str(tmp),))
        with open(tmp, "rb") as f, gzip.open(dst, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        tmp.unlink(missing_ok=True)
        for o in bdir.glob("radar-*.sqlite3"):          # несжатые копии прежних версий — по 30–60 МБ каждая
            o.unlink(missing_ok=True)
        for o in sorted(bdir.glob("radar-*.sqlite3.gz"))[:-keep]:
            o.unlink(missing_ok=True)
        return dst

    def compact(self, vacuum: bool | None = None) -> dict:
        """Чистка: шум (не прошёл фильтр) через 3 дня — без текста и поиска, через 14 — удалить; разобранное ИИ, но
        «не про нас», через 30 дней — без текста. Всё значимое (P1–P3, алерты, решения операторов) хранится полностью.
        Затем — сжатие файла базы, если пустого места набралось много (без него файл не уменьшается)."""
        st = {}
        d3, d14, d30 = ago(days=3), ago(days=14), ago(days=30)
        st["deleted"] = self.x("DELETE FROM items WHERE candidate=0 AND collected_at<?", (d14,))
        st["stripped"] = self.x(
            "UPDATE items SET text=substr(text, 1, 280), parent_text=NULL, match_json=NULL, match_hint=NULL, media_json=NULL "
            "WHERE candidate=0 AND collected_at<? AND (length(text)>280 OR parent_text IS NOT NULL OR match_json IS NOT NULL "
            "OR media_json IS NOT NULL)", (d3,))
        self.x("DELETE FROM items_fts WHERE rowid IN (SELECT id FROM items WHERE candidate=0 AND collected_at<?)", (d3,))
        st["archived"] = self.x(
            "UPDATE items SET text=substr(text, 1, 400), parent_text=NULL, match_json=NULL, media_json=NULL "
            "WHERE candidate=1 AND (relevant=0 OR priority='P4') AND workflow='open' AND analyzed_at<? "
            "AND (length(text)>400 OR match_json IS NOT NULL)", (d30,))
        self.x("DELETE FROM items_fts WHERE rowid NOT IN (SELECT id FROM items)")
        self.x("DELETE FROM metrics WHERE ts<?", (ago(days=14),))
        self.x("DELETE FROM events WHERE ts<?", (d30,))
        self.x("DELETE FROM outbox WHERE status IN ('sent','dropped','dead') AND created_at<?", (d14,))
        with self._lock:
            pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
            free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
            st["free_pct"] = round(100 * free / max(1, pages))
            if vacuum or (vacuum is None and free > 0.15 * pages):
                self.conn.execute("VACUUM")
                st["vacuum"] = True
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return st
