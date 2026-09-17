"""SQLite event store. One file, queryable, easy to back up."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from .config import db_path as _default_db_path
from .models import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    content_hash TEXT PRIMARY KEY,
    section      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    summary      TEXT,
    source_title TEXT,
    source_id    TEXT,
    published    INTEGER NOT NULL,
    fetched_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_section_pub ON items(section, published);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published);

CREATE TABLE IF NOT EXISTS sent_log (
    content_hash TEXT NOT NULL,
    section      TEXT NOT NULL,
    sent_at      INTEGER NOT NULL,
    PRIMARY KEY (content_hash, section)
);

-- Round-robin cursor for rotating deep-dive slots (see passband/cadence.py).
CREATE TABLE IF NOT EXISTS rotation_state (
    grp         TEXT PRIMARY KEY,
    cursor      INTEGER NOT NULL,
    updated_day TEXT
);

-- Populated in the dashboard phase (structured risk feeds). Created now so the
-- schema is stable.
CREATE TABLE IF NOT EXISTS risk_events (
    id         TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    title      TEXT NOT NULL,
    severity   REAL,
    lat        REAL,
    lon        REAL,
    occurred_at INTEGER,
    source     TEXT,
    url        TEXT,
    raw        TEXT,
    fetched_at INTEGER NOT NULL
);
"""


@contextmanager
def connect(db_path: Path | str | None = None):
    # Resolved here, not as a default argument: a default is bound once at
    # import and would ignore PASSBAND_DB_PATH set afterwards.
    path = Path(db_path) if db_path is not None else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL: one writer (gather) + many concurrent readers (the MCP server) without
    # blocking. journal_mode persists on the file once set, so readers inherit it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect_read(db_path: Path | str | None = None):
    """Read-only access for concurrent consumers (e.g. the MCP server).

    Does not create the schema or commit — the gather writer owns the DB.

    Opened through a ``file:...?mode=ro`` URI rather than a plain path, which
    buys two things a bare connection did not. SQLite enforces the read-only
    part, so "only issues SELECTs" is a guarantee instead of a convention. And
    a missing file fails here with a clear message, where a plain connect would
    CREATE an empty 0-byte DB and then fail on the first query with
    ``no such table: items`` — the confusing shape this takes when the MCP
    server starts before the first gather.
    """
    path = Path(db_path) if db_path is not None else _default_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No Passband store at {path}. The `gather` job creates it; "
            f"run `python main.py gather` before starting a reader."
        )
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


def upsert_items(conn: sqlite3.Connection, items: Iterable[Item]) -> int:
    now = int(time.time())
    rows = [
        (i.content_hash, i.section, i.title, i.url, i.summary,
         i.source_title, i.source_id, i.published, now)
        for i in items
    ]
    cur = conn.executemany(
        """INSERT INTO items
           (content_hash, section, title, url, summary, source_title, source_id, published, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(content_hash) DO NOTHING""",
        rows,
    )
    return cur.rowcount


def items_for_buckets(
    conn: sqlite3.Connection,
    buckets: list[str],
    since_epoch: int,
    exclude_sent: bool = True,
) -> list[Item]:
    """Items across one or more buckets (composite sections pass several).

    exclude_sent checks sent_log against the item's OWN bucket, so an item
    surfaced by a composite section (e.g. daily `tech` or `top_news`) is also
    suppressed from the deep-dive that shares its bucket, and vice versa.
    """
    if not buckets:
        return []
    placeholders = ",".join("?" for _ in buckets)
    q = f"""
        SELECT i.* FROM items i
        WHERE i.section IN ({placeholders}) AND i.published >= ?
    """
    params = [*buckets, since_epoch]
    if exclude_sent:
        q += """ AND NOT EXISTS (
                 SELECT 1 FROM sent_log s
                 WHERE s.content_hash = i.content_hash AND s.section = i.section)"""
    q += " ORDER BY i.published DESC"
    out = []
    for r in conn.execute(q, params):
        out.append(Item(
            title=r["title"], url=r["url"], summary=r["summary"] or "",
            section=r["section"], source_title=r["source_title"] or "",
            source_id=r["source_id"] or "", published=r["published"],
            content_hash=r["content_hash"],
        ))
    return out


def items_for_section(
    conn: sqlite3.Connection,
    section: str,
    since_epoch: int,
    exclude_sent: bool = True,
) -> list[Item]:
    return items_for_buckets(conn, [section], since_epoch, exclude_sent)


def recent_items(
    conn: sqlite3.Connection,
    since_epoch: int,
    section: str | None = None,
    limit: int = 200,
) -> list[Item]:
    """Most-recent items since `since_epoch`, optionally filtered to one section.
    Section-agnostic recency query for the MCP reader (no sent_log filtering)."""
    if section:
        q = ("SELECT * FROM items WHERE published >= ? AND section = ? "
             "ORDER BY published DESC LIMIT ?")
        params = [since_epoch, section, limit]
    else:
        q = ("SELECT * FROM items WHERE published >= ? "
             "ORDER BY published DESC LIMIT ?")
        params = [since_epoch, limit]
    out = []
    for r in conn.execute(q, params):
        out.append(Item(
            title=r["title"], url=r["url"], summary=r["summary"] or "",
            section=r["section"], source_title=r["source_title"] or "",
            source_id=r["source_id"] or "", published=r["published"],
            content_hash=r["content_hash"],
        ))
    return out


def mark_sent(conn: sqlite3.Connection, hashes: Iterable[str], section: str) -> None:
    now = int(time.time())
    conn.executemany(
        "INSERT OR IGNORE INTO sent_log (content_hash, section, sent_at) VALUES (?,?,?)",
        [(h, section, now) for h in hashes],
    )


def counts(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    by_section = {
        r["section"]: r["n"]
        for r in conn.execute("SELECT section, COUNT(*) n FROM items GROUP BY section")
    }
    return {"total": total, "by_section": by_section}


def window_counts(
    conn: sqlite3.Connection,
    buckets: list[str],
    now_epoch: int,
    days: int,
) -> list[int]:
    """Item counts per trailing 24h window over `days`+1 windows.

    Index 0 = the last 24h, index 1 = the 24h before that, etc. Windows with
    no items are filled with 0 so sparse feeds get a true (low) baseline.
    Anchoring windows to "now" rather than calendar days keeps the comparison
    honest for an early-morning send (a calendar 'today' would be ~6h wide).
    """
    if not buckets:
        return [0] * (days + 1)
    placeholders = ",".join("?" for _ in buckets)
    since = now_epoch - (days + 1) * 86400
    rows = conn.execute(
        f"""SELECT CAST((? - published) / 86400 AS INTEGER) AS win, COUNT(*) AS n
            FROM items
            WHERE section IN ({placeholders}) AND published >= ? AND published <= ?
            GROUP BY win""",
        [now_epoch, *buckets, since, now_epoch],
    ).fetchall()
    counts_ = [0] * (days + 1)
    for r in rows:
        if 0 <= r["win"] <= days:
            counts_[r["win"]] = r["n"]
    return counts_


def history_days(conn: sqlite3.Connection, buckets: list[str], now_epoch: int) -> int:
    """How many days back the oldest stored item in these buckets reaches."""
    if not buckets:
        return 0
    placeholders = ",".join("?" for _ in buckets)
    row = conn.execute(
        f"SELECT MIN(published) AS oldest FROM items WHERE section IN ({placeholders})",
        buckets,
    ).fetchone()
    if not row or not row["oldest"]:
        return 0
    return max(0, (now_epoch - row["oldest"]) // 86400)


def upsert_risk_events(conn: sqlite3.Connection, events: Iterable[dict]) -> int:
    """Store structured alert events (USGS/NWS). Idempotent on event id."""
    now = int(time.time())
    rows = [
        (e["id"], e["category"], e["title"], e.get("severity"),
         e.get("lat"), e.get("lon"), e.get("occurred_at"),
         e.get("source"), e.get("url"), e.get("raw"), now)
        for e in events
    ]
    cur = conn.executemany(
        """INSERT INTO risk_events
           (id, category, title, severity, lat, lon, occurred_at, source, url, raw, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO NOTHING""",
        rows,
    )
    return cur.rowcount


def rotation_cursor(conn: sqlite3.Connection, grp: str) -> int:
    row = conn.execute(
        "SELECT cursor FROM rotation_state WHERE grp = ?", (grp,)
    ).fetchone()
    return row["cursor"] if row else 0


def set_rotation_cursor(conn: sqlite3.Connection, grp: str, cursor: int, day: str) -> None:
    conn.execute(
        """INSERT INTO rotation_state (grp, cursor, updated_day) VALUES (?,?,?)
           ON CONFLICT(grp) DO UPDATE SET cursor = excluded.cursor,
                                          updated_day = excluded.updated_day""",
        (grp, cursor, day),
    )


def already_sent(conn: sqlite3.Connection, content_hash: str, section: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sent_log WHERE content_hash = ? AND section = ?",
        (content_hash, section),
    ).fetchone()
    return row is not None
