"""Storage layer — schema + connection lifecycle over a pluggable backend.

The SQL dialect lives in :mod:`morphdb.backend`, while
:mod:`morphdb.storage` exposes the logical operations used by domain code.
Backends can target SQLite (default, zero-dependency), PostgreSQL
(``pip install morphdb[postgres]``), or DynamoDB
(``pip install morphdb[dynamodb]``).

A single connection guarded by a reentrant lock serializes all access — simple
and correct at single-instance scale (see :mod:`morphdb.backend` for the
concurrency rationale and the multi-instance story).

Multi-tenancy
-------------
One MorphDB process hosts many independent **apps** (one per website). Every
type and object belongs to exactly one app, identified by an app *key*. The
``apps`` table is the tenant root; every other table carries an ``app`` column
with a ``REFERENCES apps(key) ON DELETE CASCADE`` foreign key, so deleting an
app wipes all of its schemas, objects, relations, and edges in one statement.
Foreign keys are enforced at the storage layer (SQLite ``PRAGMA foreign_keys=ON``;
Postgres always), making that cascade (and the "app must exist" check) real.

Tables
------
apps                key PK, created_at
object_schemas      (app, name) PK, fields JSON, timestamps
objects             guid PK, app, object_type, data JSON blob, timestamps
association_schemas (app, name) PK, from/to type, forward/inverse label, ...
associations        id PK, app, assoc_name, from_guid, to_guid  (one row/edge)
field_index         (object_id, field_name) PK, app, object_type, typed value cols

Within an app, type names are unique (the composite primary key enforces it);
the same name may be reused freely in a different app.

Design note — associations are stored as a single canonical row per edge (not
two mirrored rows). Bidirectional traversal is achieved by querying both the
from_guid and to_guid columns (both indexed). This avoids the dual-write
consistency hazard of mirrored rows while still letting an object discover all
of its relationships in one query.
"""

import threading
from contextlib import contextmanager

from . import backend as _backend
from .storage import DynamoStorage, SqlStorage

_LOCK = threading.RLock()
_CONN = None        # backend.Connection facade (shared, lock-guarded)
_BACKEND = None     # the active backend (SqliteBackend / PostgresBackend)
_STORAGE = None     # logical storage facade (SqlStorage / DynamoStorage)

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

-- Derived field-value index: one row per (object, scalar field), value held in a
-- typed, indexed column so filters/sorts are index-backed instead of scanning the
-- JSON blob. Purely an accelerator (objects.data stays source of truth); rebuilt
-- by morphdb.fieldindex.backfill. The ON DELETE CASCADE to objects(guid) clears an
-- object's rows when it (or, transitively, its type / app) is deleted.
CREATE TABLE IF NOT EXISTS field_index (
    app         TEXT NOT NULL,
    object_id   TEXT NOT NULL REFERENCES objects(guid) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    str_val     TEXT,
    num_val     NUMERIC,
    bool_val    INTEGER,
    PRIMARY KEY (object_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_fi_str  ON field_index(app, object_type, field_name, str_val);
CREATE INDEX IF NOT EXISTS idx_fi_num  ON field_index(app, object_type, field_name, num_val);
CREATE INDEX IF NOT EXISTS idx_fi_bool ON field_index(app, object_type, field_name, bool_val);

CREATE TABLE IF NOT EXISTS association_schemas (
    app                 TEXT NOT NULL REFERENCES apps(key) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    from_type           TEXT NOT NULL,
    to_type             TEXT NOT NULL,
    forward_name        TEXT NOT NULL,
    inverse_name        TEXT NOT NULL,
    cardinality         TEXT NOT NULL,
    "symmetric"         INTEGER NOT NULL DEFAULT 0,   -- quoted: reserved word in Postgres
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


def init_db(target):
    """Open (or create) the database at ``target`` and ensure the schema exists.

    ``target`` is a SQLite path, ``":memory:"``, or a Postgres URL
    (``postgresql://...``); see :func:`morphdb.backend.from_target`. Safe to call
    more than once; the second call replaces the connection (used by tests).
    """
    return _open(target, reset=False)


def _reset_and_init(target):
    """Wipe ``target`` and re-create a clean schema. Test-only helper used to
    give each test a fresh database on a persistent backend (Postgres)."""
    return _open(target, reset=True)


def _open(target, reset):
    """Shared open path for :func:`init_db` / :func:`_reset_and_init`.

    Closes any existing connection, opens the backend, optionally wipes it
    (``reset=True``), creates the schema, runs migrations, and installs the
    result as the process-wide connection.
    """
    global _CONN, _BACKEND, _STORAGE
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        _STORAGE = None
        be = _backend.from_target(target)
        raw = be.connect()
        if reset and be.name == "dynamodb":
            be.create_schema(raw, SCHEMA_SQL)
            be.reset(raw)
        elif reset:
            be.reset(raw)
        be.create_schema(raw, SCHEMA_SQL)
        if be.name == "dynamodb":
            conn = None
            storage = DynamoStorage(raw)
        else:
            conn = _backend.Connection(be, raw, _LOCK)
            storage = SqlStorage(conn)
            _migrate(be, raw, conn)
            raw.commit()
        _BACKEND = be
        _CONN = conn
        _STORAGE = storage
    return _STORAGE if _CONN is None else _CONN


def _migrate(be, raw, conn):
    """Guard against legacy databases and run one-time data migrations.

    The legacy guard refuses a database from before the multi-tenant 'app' model:
    apps make ``(app, name)`` a type's identity, which changes table primary keys
    — not an additive migration. Fresh databases are created app-aware by
    SCHEMA_SQL and pass straight through.

    field_index (schema version 1) is a derived accelerator added after the
    original schema; it is populated once from existing object blobs, gated by the
    backend's user-version so it runs exactly once. Purely additive — it never
    touches object blobs, so it is safe to run against live data on upgrade.
    """
    cols = be.table_columns(raw, "object_schemas")
    if cols and "app" not in cols:
        raise RuntimeError(
            "This database predates MorphDB's multi-tenant 'app' model and cannot "
            "be opened. Point at a fresh database; the app model requires a clean "
            "schema."
        )

    if be.get_user_version(raw) < 1:
        from . import fieldindex
        fieldindex.backfill(conn)
        be.set_user_version(raw, 1)


@contextmanager
def transaction():
    """Yield the shared connection inside an atomic, committed transaction.

    All multi-statement writes funnel through here so they are atomic with
    respect to each other (e.g. enforce cardinality, then insert). The shared
    lock is held for the whole block; the backend manages commit/rollback.
    """
    if _CONN is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _LOCK:
        with _BACKEND.transaction(_CONN.raw):
            yield _CONN


@contextmanager
def storage_transaction():
    """Yield the active logical storage facade inside the backend's write guard.

    SQL backends still get a real database transaction. DynamoDB currently uses
    a process-level lock plus idempotent item writes; request-level atomicity is a
    future backend optimization rather than part of the public API contract.
    """
    if _STORAGE is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _LOCK:
        raw = getattr(_CONN, "raw", None) if _CONN is not None else _STORAGE.raw
        has_log = hasattr(_STORAGE, "begin")
        if has_log:
            _STORAGE.begin()
        try:
            with _BACKEND.transaction(raw):
                yield _STORAGE
        except Exception:
            if has_log:
                _STORAGE.rollback()
            raise
        else:
            if has_log:
                _STORAGE.commit()


def conn():
    if _CONN is None:
        if _BACKEND is not None and _BACKEND.name == "dynamodb":
            raise RuntimeError(
                "The active MorphDB backend is DynamoDB, which has no SQL "
                "connection. Use db.storage() / db.storage_transaction().")
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _CONN


def storage():
    if _STORAGE is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _STORAGE


def backend():
    """The active backend (or None before init). Used by tooling that needs to
    know the engine (e.g. the dashboard / status)."""
    return _BACKEND


def like_ci():
    """The backend's case-insensitive LIKE keyword (SQLite ``LIKE`` is already
    case-insensitive; Postgres needs ``ILIKE``) — used by the ``contains`` filter."""
    return _BACKEND.like_ci() if _BACKEND is not None else "LIKE"
