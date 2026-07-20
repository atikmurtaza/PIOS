"""PIOS storage: SQLite (WAL) + FTS5. E1-owned. Plain sqlite3, no ORM."""
import os
import re
import sqlite3
import time

DB_PATH = os.environ.get("PIOS_DB") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PIOS", "pios.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    source TEXT NOT NULL,          -- 'window' | 'file' | 'chat'
    app TEXT,
    title TEXT,
    detail TEXT,
    dur_s REAL DEFAULT 0,
    episodized INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS episodes(
    id INTEGER PRIMARY KEY,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    summary TEXT NOT NULL,
    apps TEXT,                     -- csv
    source_event_ids TEXT NOT NULL -- csv, provenance (privacy invariant #4)
);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(summary);
CREATE TABLE IF NOT EXISTS facts(
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    day TEXT NOT NULL,             -- 'YYYY-MM-DD' the fact was distilled from
    text TEXT NOT NULL,
    source_episode_ids TEXT NOT NULL, -- csv, provenance (privacy invariant #4)
    superseded_by INTEGER          -- NULL = live fact
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS egress(
    id INTEGER PRIMARY KEY,
    ts REAL,
    destination TEXT,
    payload TEXT,
    allowed INTEGER
);
"""


def connect(path=None):
    path = path or os.environ.get("PIOS_DB") or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread=False: FastAPI resolves the connection dependency and
    # runs the handler on different threadpool threads. Every connection is
    # request/thread-private (never used concurrently), and WAL handles
    # cross-connection concurrency.
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # NORMAL halves fsyncs vs FULL; with WAL the db can't corrupt on power
    # loss, worst case is losing the final seconds of events. Right trade for
    # an always-on tracker on a laptop SSD.
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    con.commit()
    return con


def insert_event(con, ev):
    cur = con.execute(
        "INSERT INTO events(ts, source, app, title, detail, dur_s) VALUES(?,?,?,?,?,?)",
        (ev.get("ts", time.time()), ev["source"], ev.get("app"),
         ev.get("title"), ev.get("detail"), ev.get("dur_s", 0)))
    con.commit()
    return cur.lastrowid


def recent_events(con, since_ts):
    return con.execute(
        "SELECT * FROM events WHERE ts >= ? ORDER BY ts", (since_ts,)).fetchall()


def insert_episode(con, ep):
    cur = con.execute(
        "INSERT INTO episodes(start_ts, end_ts, summary, apps, source_event_ids) "
        "VALUES(?,?,?,?,?)",
        (ep["start_ts"], ep["end_ts"], ep["summary"], ep.get("apps", ""),
         ep["source_event_ids"]))
    ep_id = cur.lastrowid
    con.execute("INSERT INTO episodes_fts(rowid, summary) VALUES(?,?)",
                (ep_id, ep["summary"]))
    con.commit()
    return ep_id


def episodes_between(con, t0, t1):
    return con.execute(
        "SELECT * FROM episodes WHERE start_ts >= ? AND start_ts <= ? "
        "ORDER BY start_ts", (t0, t1)).fetchall()


def delete_episode(con, ep_id):
    con.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))
    con.execute("DELETE FROM episodes_fts WHERE rowid = ?", (ep_id,))
    con.commit()


def _fts_query(query):
    """Sanitize arbitrary user text into a safe FTS5 query.

    Only word characters survive; each term is double-quoted so FTS5
    operators/punctuation ("what's", "C++?") can never raise. OR for recall.
    """
    terms = re.findall(r"[A-Za-z0-9_]+", query or "")
    return " OR ".join('"%s"' % t for t in terms)


ASSIST_APP = "manual-assist"   # episodes that are prior Q&A, not observed activity


def search(con, query, limit=8, assist=None):
    """FTS5 over episodes, recency-boosted.

    assist: None = every episode (what an explicit memory search wants),
    False = observed activity only, True = only prior Q&A.

    Q&A episodes are stored as episodes so the user can find them again, but
    they are not things the user *did*, and they're always the newest rows —
    left in one pooled query they took most of the LIMIT and pushed real
    activity out (evals/cases.json: multi-topic-crowdout). Two queries, each
    with its own budget, beats a rank penalty: the caller decides how much
    prior Q&A it wants instead of hoping bm25 sorts it low enough.
    """
    q = _fts_query(query)
    if not q:
        return []
    where = {None: "", False: " AND e.apps IS NOT ?", True: " AND e.apps IS ?"}[assist]
    args = [q] + ([] if assist is None else [ASSIST_APP]) + [time.time(), limit]
    # bm25 rank: lower = better. Penalize age (days, capped at 30) to boost
    # recent episodes. ponytail: naive linear recency boost, tune if it misranks.
    return con.execute(
        "SELECT e.*, f.rank AS score FROM episodes_fts f "
        "JOIN episodes e ON e.id = f.rowid "
        "WHERE episodes_fts MATCH ?" + where +
        " ORDER BY f.rank + min(max(? - e.end_ts, 0) / 86400.0, 30.0) * 0.05 "
        "LIMIT ?", args).fetchall()


def insert_fact(con, fact):
    cur = con.execute(
        "INSERT INTO facts(ts, day, text, source_episode_ids) VALUES(?,?,?,?)",
        (fact.get("ts", time.time()), fact["day"], fact["text"],
         fact["source_episode_ids"]))
    fid = cur.lastrowid
    con.execute("INSERT INTO facts_fts(rowid, text) VALUES(?,?)",
                (fid, fact["text"]))
    con.commit()
    return fid


def delete_fact(con, fid):
    con.execute("DELETE FROM facts WHERE id = ?", (fid,))
    con.execute("DELETE FROM facts_fts WHERE rowid = ?", (fid,))
    con.commit()


def facts_all(con, limit=200):
    return con.execute(
        "SELECT * FROM facts WHERE superseded_by IS NULL "
        "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


def search_facts(con, query, limit=5):
    q = _fts_query(query)
    if not q:
        return []
    return con.execute(
        "SELECT f.*, x.rank AS score FROM facts_fts x "
        "JOIN facts f ON f.id = x.rowid "
        "WHERE facts_fts MATCH ? AND f.superseded_by IS NULL "
        "ORDER BY x.rank + min(max(? - f.ts, 0) / 86400.0, 30.0) * 0.05 "
        "LIMIT ?", (q, time.time(), limit)).fetchall()


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
    con.commit()


def log_egress(con, destination, payload, allowed):
    con.execute(
        "INSERT INTO egress(ts, destination, payload, allowed) VALUES(?,?,?,?)",
        (time.time(), destination, payload, allowed))
    con.commit()


def checkpoint(con):
    """Fold the WAL into the main db file (best-effort)."""
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def egress_rows(con):
    return con.execute("SELECT * FROM egress ORDER BY ts DESC").fetchall()


def stats(con):
    ev = con.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM events"
    ).fetchone()
    n_ep = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    return {"events": ev["n"], "episodes": n_ep,
            "first_ts": ev["first_ts"], "last_ts": ev["last_ts"]}
