"""Object instances — the data the website reads and writes.

Every object has a globally unique ``_guid`` and belongs to one object type.
Stored as a JSON blob; projected through the current schema on every read so
that schema edits never require touching rows (lazy invalidation).

Output shape is flat with underscore-prefixed system fields:

    {"_guid", "_type", "_created_at", "_updated_at", <field>: <value>, ...}

Schema field names may not start with ``_`` (enforced at schema-define time),
so there is never a collision between system fields and user fields.
"""

import json

from . import db
from .errors import bad_request, not_found
from .fieldtypes import project_data, validate_against_schema
from .schema import get_object_schema
from .util import new_guid, now_iso

RESERVED_QUERY_KEYS = {"limit", "offset", "sort", "order", "expand"}
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"}


def _project(row, fields):
    data = project_data(json.loads(row["data"]), fields)
    out = {
        "_guid": row["guid"],
        "_type": row["object_type"],
        "_created_at": row["created_at"],
        "_updated_at": row["updated_at"],
    }
    out.update(data)
    return out


# --- writes -------------------------------------------------------------------


def create_object(object_type, data):
    schema = get_object_schema(object_type, required=True)
    clean = validate_against_schema(data or {}, schema["fields"], partial=False)
    guid = new_guid(object_type)
    ts = now_iso()
    with db.transaction() as c:
        c.execute(
            "INSERT INTO objects (guid, object_type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guid, object_type, json.dumps(clean), ts, ts),
        )
        row = c.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone()
    return _project(row, schema["fields"])


def upsert_object(object_type, guid, data, partial=True):
    """Create-or-update an object at a caller-supplied guid.

    ``partial`` (default) merges the provided fields into the existing blob, so
    a caller can patch one field. With ``partial=False`` the object's data is
    fully replaced by ``data``.
    """
    schema = get_object_schema(object_type, required=True)
    ts = now_iso()
    with db.transaction() as c:
        existing = c.execute(
            "SELECT * FROM objects WHERE guid = ?", (guid,)
        ).fetchone()

        if existing is not None and existing["object_type"] != object_type:
            raise bad_request(
                f"Object '{guid}' already exists with type "
                f"'{existing['object_type']}', not '{object_type}'."
            )

        # PUT (partial=False) is replace semantics and must re-validate required
        # fields, even when the object already exists. PATCH (partial=True) only
        # touches the provided fields.
        clean = validate_against_schema(
            data or {}, schema["fields"], partial=partial
        )

        if existing is None:
            blob = clean
            c.execute(
                "INSERT INTO objects (guid, object_type, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guid, object_type, json.dumps(blob), ts, ts),
            )
        else:
            blob = json.loads(existing["data"]) if partial else {}
            blob.update(clean)
            c.execute(
                "UPDATE objects SET data = ?, updated_at = ? WHERE guid = ?",
                (json.dumps(blob), ts, guid),
            )
        row = c.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone()
    return _project(row, schema["fields"])


def delete_object(guid):
    with db.transaction() as c:
        row = c.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone()
        if row is None:
            raise not_found(f"No object with guid '{guid}'.")
        c.execute("DELETE FROM objects WHERE guid = ?", (guid,))
        c.execute(
            "DELETE FROM associations WHERE from_guid = ? OR to_guid = ?",
            (guid, guid),
        )
    return {"deleted": guid}


# --- reads --------------------------------------------------------------------


def get_object(guid, object_type=None):
    row = db.conn().execute(
        "SELECT * FROM objects WHERE guid = ?", (guid,)
    ).fetchone()
    if row is None:
        raise not_found(f"No object with guid '{guid}'.")
    if object_type is not None and row["object_type"] != object_type:
        raise not_found(
            f"Object '{guid}' is of type '{row['object_type']}', not '{object_type}'."
        )
    schema = get_object_schema(row["object_type"], required=True)
    return _project(row, schema["fields"])


def _parse_filter_key(key, fields):
    if "__" in key:
        field, op = key.rsplit("__", 1)
        if op not in _OPS:
            # treat the whole thing as a field name with default eq
            field, op = key, "eq"
    else:
        field, op = key, "eq"
    if field not in fields:
        raise bad_request(
            f"Cannot filter on unknown field '{field}'. "
            f"Declared fields: {sorted(fields)}."
        )
    return field, op


def _coerce_filter_value(field, op, raw, ftype):
    from .fieldtypes import coerce_value

    if op == "exists":
        # raw is "true"/"false"
        return coerce_value(field, raw, "boolean")
    if ftype == "json":
        raise bad_request(f"Cannot filter on json field '{field}'.")
    if op == "in":
        parts = raw.split(",") if isinstance(raw, str) else list(raw)
        return [coerce_value(field, p, ftype) for p in parts]
    if op == "contains":
        return str(raw)
    return coerce_value(field, raw, ftype)


_INT64_MAX = 2 ** 63 - 1
_INT64_MIN = -(2 ** 63)


def _safe_bind(v):
    """Make a value safe to bind as a SQLite parameter.

    Python ints outside the signed-64-bit range cannot be bound (sqlite3 raises
    OverflowError). Such magnitudes are beyond SQLite's integer precision
    anyway, so we bind them as floats rather than 500 on a client value.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and (v > _INT64_MAX or v < _INT64_MIN):
        return float(v)
    return v


def _field_expr(field, fdef, params):
    """SQL expression for a field that mirrors read-time projection exactly.

    Reads return the stored value as-is and fall back to the default only when
    the key is *absent* from the blob. We reproduce that with json_type (which
    is NULL for a missing path but 'null' for a stored JSON null), so a stored
    null is never replaced by the default — keeping reads and queries in lockstep.

    Any parameter the expression needs (the default) is appended to ``params``
    in evaluation order, so the caller must add comparison params *after*.
    """
    raw = f"json_extract(data, '$.{field}')"
    default = fdef.get("default")
    if default is not None:
        params.append(_safe_bind(default))
        return (f"(CASE WHEN json_type(data, '$.{field}') IS NULL "
                f"THEN ? ELSE {raw} END)")
    return raw


def _build_where(filters, fields):
    """Translate a ``{key: value}`` filter mapping into a SQL WHERE fragment.

    Supports operators via ``field__op`` keys: eq, ne, gt, gte, lt, lte,
    contains, in, exists.
    """
    clauses = []
    params = []
    for key, raw in filters.items():
        field, op = _parse_filter_key(key, fields)
        fdef = fields[field]
        ftype = fdef["type"]
        val = _coerce_filter_value(field, op, raw, ftype)

        # Empty IN matches nothing; handle before building expr so we don't emit
        # an orphan default parameter.
        if op == "in" and not val:
            clauses.append("0")
            continue

        # expr is used exactly once per clause (so its COALESCE default, if any,
        # maps to exactly one bound parameter).
        expr = _field_expr(field, fdef, params)
        if op == "eq":
            clauses.append(f"{expr} = ?")
            params.append(_safe_bind(val))
        elif op == "ne":
            # Null-safe inequality: also matches rows where the value is null.
            clauses.append(f"{expr} IS NOT ?")
            params.append(_safe_bind(val))
        elif op == "gt":
            clauses.append(f"{expr} > ?")
            params.append(_safe_bind(val))
        elif op == "gte":
            clauses.append(f"{expr} >= ?")
            params.append(_safe_bind(val))
        elif op == "lt":
            clauses.append(f"{expr} < ?")
            params.append(_safe_bind(val))
        elif op == "lte":
            clauses.append(f"{expr} <= ?")
            params.append(_safe_bind(val))
        elif op == "contains":
            # Escape LIKE metacharacters so the match is a literal substring,
            # not a wildcard pattern.
            esc = (str(val).replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
            clauses.append(f"{expr} LIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")
        elif op == "in":
            qmarks = ",".join("?" * len(val))
            clauses.append(f"{expr} IN ({qmarks})")
            params.extend(_safe_bind(v) for v in val)
        elif op == "exists":
            clauses.append(f"{expr} IS NOT NULL" if val else f"{expr} IS NULL")
    return clauses, params


def list_objects(object_type, filters=None, limit=DEFAULT_LIMIT, offset=0,
                 sort=None, order="asc"):
    schema = get_object_schema(object_type, required=True)
    fields = schema["fields"]
    filters = filters or {}

    clauses = ["object_type = ?"]
    params = [object_type]
    fc, fp = _build_where(filters, fields)
    clauses.extend(fc)
    params.extend(fp)
    where = " AND ".join(clauses)

    order_l = str(order).lower()
    if order_l not in ("asc", "desc"):
        raise bad_request(f"Invalid order '{order}'. Use 'asc' or 'desc'.")
    order_sql = "DESC" if order_l == "desc" else "ASC"
    # Always append `guid ASC` as a deterministic tie-break so pagination is
    # stable even when the primary sort key has duplicate values. The sort key
    # uses the same projection-aware expression as filters/reads so it orders by
    # the values the client actually sees (defaults included).
    sort_params = []
    if sort:
        if sort in ("_created_at", "_updated_at", "_guid"):
            col = {"_created_at": "created_at", "_updated_at": "updated_at",
                   "_guid": "guid"}[sort]
            order_clause = f"{col} {order_sql}, guid ASC"
        elif sort in fields:
            if fields[sort]["type"] == "json":
                raise bad_request(f"Cannot sort on json field '{sort}'.")
            sort_expr = _field_expr(sort, fields[sort], sort_params)
            order_clause = f"{sort_expr} {order_sql}, guid ASC"
        else:
            raise bad_request(f"Cannot sort on unknown field '{sort}'.")
    else:
        order_clause = f"created_at {order_sql}, guid ASC"

    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        raise bad_request("limit and offset must be integers.")
    if limit < 0 or offset < 0:
        raise bad_request("limit and offset must be non-negative.")
    if offset > _INT64_MAX:
        raise bad_request("offset is too large.")
    limit = min(limit, MAX_LIMIT)

    c = db.conn()
    total = c.execute(
        f"SELECT COUNT(*) AS n FROM objects WHERE {where}", params
    ).fetchone()["n"]
    rows = c.execute(
        f"SELECT * FROM objects WHERE {where} ORDER BY {order_clause} "
        f"LIMIT ? OFFSET ?",
        params + sort_params + [limit, offset],
    ).fetchall()

    return {
        "objects": [_project(r, fields) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
