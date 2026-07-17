"""Storage layer — schema + connection lifecycle over a pluggable engine.

The SQL dialect lives in :mod:`morphdb.backend`, while :mod:`morphdb.storage`
exposes the logical store operations used by domain code. Engines can target
SQLite (default, zero-dependency), PostgreSQL
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

from . import backend as _engine_mod
from .storage import DynamoStore, SqlStore

_LOCK = threading.RLock()
_CONN = None       # backend.Connection facade (shared, lock-guarded)
_ENGINE = None     # active DatabaseEngine (SqliteEngine / PostgresEngine / DynamoEngine)
_STORE = None      # logical store facade (SqlStore / DynamoStore)

# --- change-publish seam (consumed by morphdb.streams) ------------------------
# Write handlers assemble change records inside their transaction and stage them
# via stage_change(); store_transaction publishes the batch post-commit, while
# the storage lock is still held, so records reach the consumer in commit order
# and a rolled-back write never publishes. db never imports streams — the
# consumer installs itself with set_publish_hook().
_PUBLISH_HOOK = None   # fn(records), called post-commit under _LOCK
_INTERESTED = None     # fn(app, types) -> bool; handlers gate assembly on it
_CHANGE_SEQ = 0        # global change counter; incremented under _LOCK
_TXN_DEPTH = 0         # store_transaction nesting depth (publish at outermost)
_STAGED = []           # records staged by the current outermost transaction

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
    give each test a fresh database on a persistent engine (Postgres)."""
    return _open(target, reset=True)


def _open(target, reset):
    """Shared open path for :func:`init_db` / :func:`_reset_and_init`.

    Closes any existing connection, opens the engine, optionally wipes it
    (``reset=True``), creates the schema, runs migrations, and installs the
    result as the process-wide connection.
    """
    global _CONN, _ENGINE, _STORE
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None
        _STORE = None
        global _TXN_DEPTH, _CHANGE_SEQ
        _TXN_DEPTH = 0
        _CHANGE_SEQ = 0
        _STAGED.clear()
        engine = _engine_mod.from_target(target)
        raw = engine.connect()
        if reset and engine.name == "dynamodb":
            engine.create_schema(raw, SCHEMA_SQL)
            engine.reset(raw)
        elif reset:
            engine.reset(raw)
        engine.create_schema(raw, SCHEMA_SQL)
        if engine.name == "dynamodb":
            conn = None
            store_facade = DynamoStore(raw)
        else:
            conn = _engine_mod.Connection(engine, raw, _LOCK)
            store_facade = SqlStore(conn)
            _migrate(engine, raw, conn)
            raw.commit()
        _ENGINE = engine
        _CONN = conn
        _STORE = store_facade
    return _STORE if _CONN is None else _CONN


def _migrate(engine, raw, conn):
    """Guard against legacy databases and run one-time data migrations.

    The legacy guard refuses a database from before the multi-tenant 'app' model:
    apps make ``(app, name)`` a type's identity, which changes table primary keys
    — not an additive migration. Fresh databases are created app-aware by
    SCHEMA_SQL and pass straight through.

    field_index (schema version 1) is a derived accelerator added after the
    original schema; it is populated once from existing object blobs, gated by the
    engine's user-version so it runs exactly once. Purely additive — it never
    touches object blobs, so it is safe to run against live data on upgrade.
    """
    cols = engine.table_columns(raw, "object_schemas")
    if cols and "app" not in cols:
        raise RuntimeError(
            "This database predates MorphDB's multi-tenant 'app' model and cannot "
            "be opened. Point at a fresh database; the app model requires a clean "
            "schema."
        )

    if engine.get_user_version(raw) < 1:
        from . import fieldindex
        fieldindex.backfill(conn)
        engine.set_user_version(raw, 1)


@contextmanager
def transaction():
    """Yield the shared connection inside an atomic, committed transaction.

    All multi-statement writes funnel through here so they are atomic with
    respect to each other (e.g. enforce cardinality, then insert). The shared
    lock is held for the whole block; the engine manages commit/rollback.
    """
    if _CONN is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _LOCK:
        with _ENGINE.transaction(_CONN.raw):
            yield _CONN


@contextmanager
def store_transaction():
    """Yield the active logical store facade inside the engine's write guard.

    SQL engines still get a real database transaction. DynamoDB currently uses
    a process-level lock plus idempotent item writes; request-level atomicity is a
    future store optimization rather than part of the public API contract.

    Staged change records are published after a successful commit, at the
    outermost transaction exit only, while the storage lock is still held — so
    publication order is commit order. A failed transaction publishes no object
    records; if anything had been staged, a single synthetic dirty-only record
    replaces it so streaming readers heal whatever a rollback left visible.
    """
    global _TXN_DEPTH
    if _STORE is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _LOCK:
        raw = getattr(_CONN, "raw", None) if _CONN is not None else _STORE.raw
        has_log = hasattr(_STORE, "begin")
        _TXN_DEPTH += 1
        try:
            if has_log:
                _STORE.begin()
            try:
                with _ENGINE.transaction(raw):
                    yield _STORE
            except Exception:
                if has_log:
                    _STORE.rollback()
                raise
            else:
                if has_log:
                    _STORE.commit()
        except Exception:
            if _TXN_DEPTH == 1 and _STAGED:
                addresses = sorted(_addresses(_STAGED))
                _STAGED.clear()
                _publish([{"dirty": [list(a) for a in addresses]}])
            raise
        else:
            if _TXN_DEPTH == 1 and _STAGED:
                records = _STAGED[:]
                _STAGED.clear()
                _publish(records)
        finally:
            _TXN_DEPTH -= 1


def set_publish_hook(fn, interested=None):
    """Install (or clear, with ``fn=None``) the post-commit change consumer.

    ``fn(records)`` runs after each successful outermost commit, under the
    storage lock. ``interested(app, types)`` lets write handlers skip record
    assembly entirely when nothing streams any of ``types``; absent, any
    installed hook receives every record.
    """
    global _PUBLISH_HOOK, _INTERESTED
    with _LOCK:
        _PUBLISH_HOOK = fn
        _INTERESTED = interested if fn is not None else None
        _STAGED.clear()


def publishing():
    """True when a publish hook is installed (the cheapest write-path gate)."""
    return _PUBLISH_HOOK is not None


def interested(app, *types):
    """Should a write handler assemble a change record touching ``types``?"""
    if _PUBLISH_HOOK is None:
        return False
    if _INTERESTED is None:
        return True
    return _INTERESTED(app, types)


def stage_change(record):
    """Queue a change record for post-commit publication (no-op without a hook).

    Must be called inside a store_transaction block. Records are dicts shaped:
      object write  {app, type, guid, verb, new_body|None, touched: [[type, guid]]}
      schema/app op {app, schema_op: {op, affected_types}}
    ``seq`` is stamped at publish time, in commit order.
    """
    if _PUBLISH_HOOK is None:
        return
    _STAGED.append(record)


def current_change_seq():
    """The last published change seq — the attach fence (§5.1 of the spec)."""
    with _LOCK:
        return _CHANGE_SEQ


def _addresses(records):
    """The (app, type) pairs a batch of records can affect: each record's own
    type plus every touched/affected type — the dirty-record address set."""
    out = set()
    for r in records:
        app = r.get("app")
        if "schema_op" in r:
            for t in r["schema_op"].get("affected_types", []):
                out.add((app, t))
        elif "type" in r:
            out.add((app, r["type"]))
            for t, _g in r.get("touched", []):
                out.add((app, t))
    return out


def _publish(records):
    global _CHANGE_SEQ
    for r in records:
        _CHANGE_SEQ += 1
        r["seq"] = _CHANGE_SEQ
    try:
        _PUBLISH_HOOK(records)
    except Exception:
        # A streaming consumer must never break a committed write.
        import traceback
        traceback.print_exc()


def conn():
    if _CONN is None:
        if _ENGINE is not None and _ENGINE.name == "dynamodb":
            raise RuntimeError(
                "The active MorphDB engine is DynamoDB, which has no SQL "
                "connection. Use db.store() / db.store_transaction().")
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _CONN


def store():
    if _STORE is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _STORE


def engine():
    """The active engine (or None before init). Used by tooling that needs to
    know the engine (e.g. the dashboard / status)."""
    return _ENGINE


def like_ci():
    """The engine's case-insensitive LIKE keyword (SQLite ``LIKE`` is already
    case-insensitive; Postgres needs ``ILIKE``) — used by the ``contains`` filter."""
    return _ENGINE.like_ci() if _ENGINE is not None else "LIKE"
