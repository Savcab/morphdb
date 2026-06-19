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

        clean = validate_against_schema(
            data or {}, schema["fields"], partial=partial or existing is not None
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


def _build_where(filters, fields):
    """Translate a ``{key: value}`` filter mapping into a SQL WHERE fragment.

    Supports operators via ``field__op`` keys: eq, ne, gt, gte, lt, lte,
    contains, in, exists.
    """
    clauses = []
    params = []
    for key, raw in filters.items():
        field, op = _parse_filter_key(key, fields)
        ftype = fields[field]["type"]
        val = _coerce_filter_value(field, op, raw, ftype)
        expr = f"json_extract(data, '$.{field}')"
        if op == "eq":
            clauses.append(f"{expr} = ?")
            params.append(val)
        elif op == "ne":
            clauses.append(f"({expr} IS NULL OR {expr} != ?)")
            params.append(val)
        elif op == "gt":
            clauses.append(f"{expr} > ?")
            params.append(val)
        elif op == "gte":
            clauses.append(f"{expr} >= ?")
            params.append(val)
        elif op == "lt":
            clauses.append(f"{expr} < ?")
            params.append(val)
        elif op == "lte":
            clauses.append(f"{expr} <= ?")
            params.append(val)
        elif op == "contains":
            clauses.append(f"{expr} LIKE ?")
            params.append(f"%{val}%")
        elif op == "in":
            if not val:
                clauses.append("0")  # empty IN matches nothing
            else:
                qmarks = ",".join("?" * len(val))
                clauses.append(f"{expr} IN ({qmarks})")
                params.extend(val)
        elif op == "exists":
            if val:
                clauses.append(f"{expr} IS NOT NULL")
            else:
                clauses.append(f"{expr} IS NULL")
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

    order_sql = "DESC" if str(order).lower() == "desc" else "ASC"
    if sort:
        if sort in ("_created_at", "_updated_at", "_guid"):
            col = {"_created_at": "created_at", "_updated_at": "updated_at",
                   "_guid": "guid"}[sort]
            order_clause = f"{col} {order_sql}"
        elif sort in fields:
            order_clause = f"json_extract(data, '$.{sort}') {order_sql}, guid ASC"
        else:
            raise bad_request(f"Cannot sort on unknown field '{sort}'.")
    else:
        order_clause = f"created_at {order_sql}, guid ASC"

    try:
        limit = max(0, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise bad_request("limit and offset must be integers.")

    c = db.conn()
    total = c.execute(
        f"SELECT COUNT(*) AS n FROM objects WHERE {where}", params
    ).fetchone()["n"]
    rows = c.execute(
        f"SELECT * FROM objects WHERE {where} ORDER BY {order_clause} "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return {
        "objects": [_project(r, fields) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
