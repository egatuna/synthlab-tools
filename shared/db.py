"""SQLite helper for synthlab-tools. One tools.db, shared tables.

Tables:
  kv        (tool, key, value)          — per-tool key/value state
  seen      (tool, item_id, first_seen) — idempotency: never process an ID twice
  runs      (tool, ts, status, detail)  — run history for digests/debugging
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "tools.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    tool TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (tool, key)
);
CREATE TABLE IF NOT EXISTS seen (
    tool TEXT NOT NULL, item_id TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (tool, item_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL, ts TEXT NOT NULL, status TEXT NOT NULL, detail TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(_SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kv_get(tool: str, key: str, default: str | None = None) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE tool=? AND key=?", (tool, key)).fetchone()
    return row[0] if row else default


def kv_set(tool: str, key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO kv (tool,key,value) VALUES (?,?,?) "
                  "ON CONFLICT(tool,key) DO UPDATE SET value=excluded.value",
                  (tool, key, value))


def seen(tool: str, item_id: str) -> bool:
    with conn() as c:
        row = c.execute("SELECT 1 FROM seen WHERE tool=? AND item_id=?", (tool, item_id)).fetchone()
    return row is not None


def mark_seen(tool: str, item_id: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO seen (tool,item_id,first_seen) VALUES (?,?,?)",
                  (tool, item_id, _now()))


def record_run(tool: str, status: str, detail: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO runs (tool,ts,status,detail) VALUES (?,?,?,?)",
                  (tool, _now(), status, detail[:2000]))
