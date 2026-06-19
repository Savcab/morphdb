"""SQLite storage layer.

A single connection guarded by a reentrant lock. MorphDB is a localhost-scale
tool; serializing access with one lock is simpler and plenty fast, and it keeps
the four logical tables consistent without per-statement transaction juggling.

Tables
------
object_schemas      name PK, fields JSON, timestamps
objects             guid PK, object_type, data JSON blob, timestamps
association_schemas name PK, from/to type, forward/inverse label, cardinality
associations        id PK, assoc_name, from_guid, to_guid   (one row per edge)

Design note — associations are stored as a single canonical row per edge (not
two mirrored rows). Bidirectional traversal is achieved by querying both the
from_guid and to_guid columns (both indexed). This avoids the dual-write
consistency hazard of mirrored rows while still letting an object discover all
of its relationships in one query.
"""

import sqlite3
import threading
from contextlib import contextmanager

_LOCK = threading.RLock()
_CONN = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS object_schemas (
    name        TEXT PRIMARY KEY,
    fields      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    guid        TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_type ON objects(object_type);

CREATE TABLE IF NOT EXISTS association_schemas (
    name         TEXT PRIMARY KEY,
    from_type    TEXT NOT NULL,
    to_type      TEXT NOT NULL,
    forward_name TEXT NOT NULL,
    inverse_name TEXT NOT NULL,
    cardinality  TEXT NOT NULL,
    symmetric    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS associations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    assoc_name TEXT NOT NULL,
    from_guid  TEXT NOT NULL,
    to_guid    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assoc_name, from_guid, to_guid)
);
CREATE INDEX IF NOT EXISTS idx_assoc_from ON associations(from_guid);
CREATE INDEX IF NOT EXISTS idx_assoc_to   ON associations(to_guid);
CREATE INDEX IF NOT EXISTS idx_assoc_name ON associations(assoc_name);
"""


def init_db(path):
    """Open (or create) the database at ``path`` and ensure the schema exists.

    ``path`` may be ``":memory:"`` for ephemeral use (tests). Safe to call more
    than once; the second call replaces the connection (used by tests).
    """
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        conn.commit()
        _CONN = conn
    return _CONN


def _migrate(conn):
    """Apply additive migrations to databases created by older versions."""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(association_schemas)").fetchall()}
    if "symmetric" not in cols:
        conn.execute(
            "ALTER TABLE association_schemas ADD COLUMN symmetric "
            "INTEGER NOT NULL DEFAULT 0"
        )


@contextmanager
def transaction():
    """Yield the shared connection inside an exclusive, committed transaction.

    All reads and writes funnel through here so that multi-statement operations
    (e.g. enforce cardinality then insert) are atomic with respect to each other.
    """
    if _CONN is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _LOCK:
        try:
            yield _CONN
            _CONN.commit()
        except Exception:
            _CONN.rollback()
            raise


def conn():
    if _CONN is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _CONN
