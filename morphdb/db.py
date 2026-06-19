"""SQLite storage layer.

A single connection guarded by a reentrant lock. MorphDB is a localhost-scale
tool; serializing access with one lock is simpler and plenty fast, and it keeps
the logical tables consistent without per-statement transaction juggling.

Multi-tenancy
-------------
One MorphDB process hosts many independent **apps** (one per website). Every
type and object belongs to exactly one app, identified by an app *key*. The
``apps`` table is the tenant root; every other table carries an ``app`` column
with a ``REFERENCES apps(key) ON DELETE CASCADE`` foreign key, so deleting an
app wipes all of its schemas, objects, relations, and edges in one statement.
``PRAGMA foreign_keys=ON`` makes that cascade (and the "app must exist" check)
real at the storage layer.

Tables
------
apps                key PK, created_at
object_schemas      (app, name) PK, fields JSON, timestamps
objects             guid PK, app, object_type, data JSON blob, timestamps
association_schemas (app, name) PK, from/to type, forward/inverse label, ...
associations        id PK, app, assoc_name, from_guid, to_guid  (one row/edge)

Within an app, type names are unique (the composite primary key enforces it);
the same name may be reused freely in a different app.

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
CREATE TABLE IF NOT EXISTS apps (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS object_schemas (
    app         TEXT NOT NULL REFERENCES apps(key) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    fields      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (app, name)
);

CREATE TABLE IF NOT EXISTS objects (
    guid        TEXT PRIMARY KEY,
    app         TEXT NOT NULL REFERENCES apps(key) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_app_type ON objects(app, object_type);

CREATE TABLE IF NOT EXISTS association_schemas (
    app                 TEXT NOT NULL REFERENCES apps(key) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    from_type           TEXT NOT NULL,
    to_type             TEXT NOT NULL,
    forward_name        TEXT NOT NULL,
    inverse_name        TEXT NOT NULL,
    cardinality         TEXT NOT NULL,
    symmetric           INTEGER NOT NULL DEFAULT 0,
    forward_description TEXT,
    inverse_description TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (app, name)
);

CREATE TABLE IF NOT EXISTS associations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app        TEXT NOT NULL REFERENCES apps(key) ON DELETE CASCADE,
    assoc_name TEXT NOT NULL,
    from_guid  TEXT NOT NULL,
    to_guid    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(app, assoc_name, from_guid, to_guid)
);
CREATE INDEX IF NOT EXISTS idx_assoc_app      ON associations(app);
CREATE INDEX IF NOT EXISTS idx_assoc_from     ON associations(from_guid);
CREATE INDEX IF NOT EXISTS idx_assoc_to       ON associations(to_guid);
CREATE INDEX IF NOT EXISTS idx_assoc_app_name ON associations(app, assoc_name);
"""
# The (app, name) primary keys on object_schemas/association_schemas index the
# app column as their leftmost prefix, so app-scoped lookups on those tables are
# already covered; objects/associations get explicit app indexes above.


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
        conn.execute("PRAGMA foreign_keys=ON;")   # enforce the app cascade + FKs
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        conn.commit()
        _CONN = conn
    return _CONN


def _migrate(conn):
    """Guard against opening a database from before the multi-tenant 'app' model.

    Apps make ``(app, name)`` the identity of a type, which changes table primary
    keys — that is not an additive ``ALTER`` we can apply in place. Rather than
    silently rehome old rows under some magic app key (and risk reinterpreting
    data), refuse with a clear message. Fresh databases (and ``:memory:``) are
    created app-aware by ``SCHEMA_SQL`` above and pass straight through.
    """
    info = conn.execute("PRAGMA table_info(object_schemas)").fetchall()
    cols = {r["name"] for r in info}
    if info and "app" not in cols:
        raise RuntimeError(
            "This database predates MorphDB's multi-tenant 'app' model and cannot "
            "be opened. Point --db at a fresh file (or remove the old one); the "
            "app model requires a clean schema."
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
