"""Field index — a derived, index-backed accelerator for object field queries.

Object field values live in one JSON ``data`` blob (see :mod:`objects`), which
SQLite cannot index: a filter like ``?status=active`` would scan every blob.
This module maintains a side table, ``field_index``, with one row per
(object, scalar field) carrying the value in a *typed, indexed* column, so the
same filter becomes an index probe.

The blob stays the single source of truth; ``field_index`` is purely derived and
can be rebuilt from the blobs at any time (see :func:`backfill`). It is kept in
lockstep with the blob *transactionally* — every object write rewrites that
object's index rows inside the same transaction as the blob write — so the two
never drift at runtime. Deletes need no code here: each row has an
``ON DELETE CASCADE`` foreign key to ``objects(guid)``, so deleting an object
(or, transitively, its type or its whole app) clears its index rows for free.

What is indexed
---------------
Indexing is opt-in per field: only a schema field declared ``"index": true``
gets rows here. The four scalar types are grouped by SQLite storage class:

    string, datetime  -> str_val   (TEXT; datetime is canonical ISO, sorts right)
    number            -> num_val   (NUMERIC; keeps big ints exact, unlike a float)
    boolean           -> bool_val  (INTEGER 0/1; kept separate from num_val so a
                                    number<->boolean retype invalidates correctly)

``json`` fields cannot be indexed (no total order; not comparable/sortable).
A filter or sort on an un-indexed field is rejected by the query layer — the
agent must mark the field ``"index": true`` first. Turning that flag on
backfills the field (:func:`reindex_field`); turning it off drops its rows
(:func:`drop_field`).

Defaults are deliberately *not* materialized here: an object missing a field, or
left at the wrong type after a retype, simply has no value in the current-type
column, and the query layer treats it as reading the field's default (mirroring
:func:`fieldtypes.project_data`). That is what keeps schema edits O(1): a retype
or default change rewrites no rows — neither blobs nor this index.
"""

import json

from .fieldtypes import _matches_type

# Field type -> the typed column that holds its value, grouped by SQLite storage
# class (the same grouping the read-time type guard uses). ``json`` is absent.
_COLUMN = {
    "string": "str_val",
    "datetime": "str_val",
    "number": "num_val",
    "boolean": "bool_val",
}
_VALUE_COLUMNS = ("str_val", "num_val", "bool_val")

_INT64_MAX = 2 ** 63 - 1
_INT64_MIN = -(2 ** 63)

_INSERT = (
    "INSERT INTO field_index "
    "(app, object_id, object_type, field_name, str_val, num_val, bool_val) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def column_for_type(ftype):
    """The ``field_index`` column for a field type, or ``None`` if not indexed."""
    return _COLUMN.get(ftype)


def _bind_number(v):
    # Ints beyond signed 64-bit can't bind to sqlite3 (OverflowError) and are
    # past SQLite's integer precision anyway; store as float — matching
    # objects._safe_bind so an indexed value and a query value compare equal.
    if isinstance(v, int) and not isinstance(v, bool) and (v > _INT64_MAX or v < _INT64_MIN):
        return float(v)
    return v


def index_rows_for(app, object_type, guid, blob, fields):
    """Build the ``field_index`` rows for one object's stored blob.

    One row per scalar field that is present in the blob *and* whose stored value
    still matches the field's current type (the same rule the read path applies).
    Absent / wrong-type / null / json fields produce no row — the query layer
    reads them as the field's default. Each row populates exactly one value
    column (per the field's type); the others stay NULL.
    """
    rows = []
    for name, fdef in fields.items():
        if not fdef.get("index"):             # only fields opted into indexing
            continue
        col = _COLUMN.get(fdef["type"])
        if col is None:                       # json can't be indexed
            continue
        if name not in blob:                  # absent -> reads as default
            continue
        v = blob[name]
        if v is None or not _matches_type(v, fdef["type"]):
            continue                          # null / stale-after-retype -> default
        if col == "num_val":
            v = _bind_number(v)
        vals = {c: None for c in _VALUE_COLUMNS}
        vals[col] = v
        rows.append((app, guid, object_type, name,
                     vals["str_val"], vals["num_val"], vals["bool_val"]))
    return rows


def apply_index_writes(c, app, object_type, guid, blob, fields):
    """Rewrite one object's index rows inside the caller's transaction ``c``.

    Delete-then-insert, so an update fully replaces the object's prior rows. Call
    this in the same transaction as the blob write; the two then commit together
    and can never drift.
    """
    c.execute("DELETE FROM field_index WHERE object_id = ?", (guid,))
    rows = index_rows_for(app, object_type, guid, blob, fields)
    if rows:
        c.executemany(_INSERT, rows)


def reindex_field(c, app, object_type, field_name, fdef):
    """Backfill one field's index rows from existing object blobs.

    Used when a field's ``index`` flag is turned on (or an already-indexed field
    is retyped, moving its value to a different column). Bounded to one field of
    one type — a one-time scan, like ``CREATE INDEX``. Idempotent: clears the
    field's rows first, then rebuilds. A ``json`` field is a no-op (not indexable).
    """
    c.execute("DELETE FROM field_index WHERE app = ? AND object_type = ? AND field_name = ?",
              (app, object_type, field_name))
    col = _COLUMN.get(fdef["type"])
    if col is None:
        return 0
    objs = c.execute(
        "SELECT guid, data FROM objects WHERE app = ? AND object_type = ?",
        (app, object_type)).fetchall()
    rows = []
    for r in objs:
        try:
            blob = json.loads(r["data"])
        except Exception:
            continue
        if field_name not in blob:
            continue
        v = blob[field_name]
        if v is None or not _matches_type(v, fdef["type"]):
            continue
        if col == "num_val":
            v = _bind_number(v)
        vals = {cc: None for cc in _VALUE_COLUMNS}
        vals[col] = v
        rows.append((app, r["guid"], object_type, field_name,
                     vals["str_val"], vals["num_val"], vals["bool_val"]))
    if rows:
        c.executemany(_INSERT, rows)
    return len(rows)


def drop_field(c, app, object_type, field_name):
    """Delete one field's index rows (its ``index`` flag was turned off, or the
    field was removed from the schema). Instant — no scan."""
    c.execute("DELETE FROM field_index WHERE app = ? AND object_type = ? AND field_name = ?",
              (app, object_type, field_name))


def backfill(c, app=None):
    """(Re)build ``field_index`` from the object blobs, optionally one app only.

    Used by the one-time migration that introduces the table and by
    ``morphdb reindex`` (repair, or after a default change). The blob is the
    source of truth, so this is always safe to re-run. Returns the number of
    objects scanned. Robust to a single malformed blob: it is skipped, never
    aborting the rebuild (this runs inside ``init_db`` — a raise there would stop
    the daemon from starting).
    """
    if app is not None:
        c.execute("DELETE FROM field_index WHERE app = ?", (app,))
        objs = c.execute(
            "SELECT guid, app, object_type, data FROM objects WHERE app = ?", (app,)
        ).fetchall()
    else:
        c.execute("DELETE FROM field_index")
        objs = c.execute("SELECT guid, app, object_type, data FROM objects").fetchall()

    schema_cache = {}
    scanned = 0
    for r in objs:
        key = (r["app"], r["object_type"])
        fields = schema_cache.get(key)
        if fields is None:
            srow = c.execute(
                "SELECT fields FROM object_schemas WHERE app = ? AND name = ?", key
            ).fetchone()
            fields = json.loads(srow["fields"]) if srow else {}
            schema_cache[key] = fields
        try:
            rows = index_rows_for(r["app"], r["object_type"], r["guid"],
                                  json.loads(r["data"]), fields)
            if rows:
                c.executemany(_INSERT, rows)
            scanned += 1
        except Exception:
            continue
    return scanned
