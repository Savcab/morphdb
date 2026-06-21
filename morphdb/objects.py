"""Object instances — the data the website reads and writes.

Every object has a globally unique ``_guid`` and belongs to one object type
inside one **app**. Raw field values are stored as a JSON blob and projected
through the current schema on every read (lazy invalidation), so schema edits
never touch rows. Relations are *not* in the blob — they live as edges (see
:mod:`associations`) and are folded into the object body on read / exploded into
edges on write, so the frontend treats a link like any other field.

Every read and write is scoped to the caller's app: a guid that belongs to a
different app is treated as not found, so apps are fully isolated even though
guids are globally unique.

Output shape is flat with underscore-prefixed system fields:

    {"_guid", "_type", "_created_at", "_updated_at", <field>: <value>,
     <relation>: <neighbor-guid | [neighbor-guids]>, ...}

Schema field/relation names may not start with ``_`` (enforced at schema-define
time), so there is never a collision with system fields.
"""

import json

from . import associations as assoc
from . import db
from . import fieldindex
from .errors import bad_request, not_found
from .fieldtypes import project_data, validate_against_schema
from .schema import get_object_schema
from .util import new_guid, now_iso

RESERVED_QUERY_KEYS = {"limit", "offset", "sort", "order"}
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"}


def _project_fields(row, fields):
    data = project_data(json.loads(row["data"]), fields)
    out = {
        "_guid": row["guid"],
        "_type": row["object_type"],
        "_created_at": row["created_at"],
        "_updated_at": row["updated_at"],
    }
    out.update(data)
    return out


def _project_full(app, row, fields, object_type):
    """A single object's full body: system fields + fields + relation values."""
    out = _project_fields(row, fields)
    rels = assoc.project_relations(app, [row["guid"]], object_type)[row["guid"]]
    out.update(rels)
    return out


def _split_body(app, object_type, body, fields, c=None):
    """Partition an incoming write into (field values, relation values).

    Keys that are neither a declared field nor a relation (and not a system
    ``_`` key echoed back from a read) are rejected, so typos surface early.
    """
    rel_keys = assoc.relation_keys(app, object_type, c)
    field_part, rel_part = {}, {}
    for k, v in body.items():
        if k in fields:
            field_part[k] = v
        elif k in rel_keys:
            rel_part[k] = v
        elif k.startswith("_"):
            continue  # tolerate _guid/_type echoed back from a prior read
        else:
            raise bad_request(
                f"Unknown field/relation '{k}' on type '{object_type}'. "
                f"Fields: {sorted(fields)}; relations: {sorted(rel_keys)}. "
                "Update the schema first, or remove the stray key."
            )
    return field_part, rel_part


# --- writes -------------------------------------------------------------------


def create_object(app, object_type, data):
    schema = get_object_schema(app, object_type, required=True)
    fields = schema["fields"]
    guid = new_guid(object_type)
    ts = now_iso()
    with db.transaction() as c:
        field_part, rel_part = _split_body(app, object_type, data or {}, fields, c)
        clean = validate_against_schema(field_part, fields, partial=False)
        c.execute(
            "INSERT INTO objects (guid, app, object_type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guid, app, object_type, json.dumps(clean), ts, ts),
        )
        fieldindex.apply_index_writes(c, app, object_type, guid, clean, fields)
        if rel_part:
            assoc.apply_relation_writes(c, app, guid, object_type, rel_part)
        row = c.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone()
    return _project_full(app, row, fields, object_type)


def upsert_object(app, object_type, guid, data, partial=True):
    """Create-or-update an object at a caller-supplied guid, within ``app``.

    Fields follow ``partial``: PATCH (partial=True) merges the given fields, PUT
    (partial=False) replaces the field set (and re-checks required fields).
    Relations are always patch-style: only relation keys present in the body are
    touched; each present relation's value becomes its full set (set-as-field).
    """
    schema = get_object_schema(app, object_type, required=True)
    fields = schema["fields"]
    ts = now_iso()
    with db.transaction() as c:
        existing = c.execute(
            "SELECT * FROM objects WHERE guid = ?", (guid,)
        ).fetchone()
        if existing is not None and existing["app"] != app:
            # The guid is owned by another app; from here it simply doesn't exist.
            raise not_found(f"No object with guid '{guid}'.")
        if existing is not None and existing["object_type"] != object_type:
            raise bad_request(
                f"Object '{guid}' already exists with type "
                f"'{existing['object_type']}', not '{object_type}'."
            )

        field_part, rel_part = _split_body(app, object_type, data or {}, fields, c)
        clean = validate_against_schema(field_part, fields, partial=partial)

        if existing is None:
            c.execute(
                "INSERT INTO objects (guid, app, object_type, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guid, app, object_type, json.dumps(clean), ts, ts),
            )
            stored = clean
        else:
            blob = json.loads(existing["data"]) if partial else {}
            blob.update(clean)
            c.execute(
                "UPDATE objects SET data = ?, updated_at = ? WHERE guid = ?",
                (json.dumps(blob), ts, guid),
            )
            stored = blob
        # Rebuild this object's index rows from its full stored blob, in the same
        # transaction as the blob write (delete-then-insert inside apply_index_writes).
        fieldindex.apply_index_writes(c, app, object_type, guid, stored, fields)
        if rel_part:
            assoc.apply_relation_writes(c, app, guid, object_type, rel_part)
        row = c.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone()
    return _project_full(app, row, fields, object_type)


def delete_object(app, guid):
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM objects WHERE app = ? AND guid = ?", (app, guid)
        ).fetchone()
        if row is None:
            raise not_found(f"No object with guid '{guid}'.")
        c.execute("DELETE FROM objects WHERE app = ? AND guid = ?", (app, guid))
        # field_index rows for this object are removed automatically by the
        # ON DELETE CASCADE foreign key (field_index.object_id -> objects.guid).
        # Remove this object's edges only; neighbor objects are left intact.
        c.execute(
            "DELETE FROM associations WHERE app = ? AND (from_guid = ? OR to_guid = ?)",
            (app, guid, guid),
        )
    return {"deleted": guid}


# --- reads --------------------------------------------------------------------


def get_object(app, guid, object_type=None, include=None):
    row = db.conn().execute(
        "SELECT * FROM objects WHERE app = ? AND guid = ?", (app, guid)
    ).fetchone()
    if row is None:
        raise not_found(f"No object with guid '{guid}'.")
    if object_type is not None and row["object_type"] != object_type:
        raise not_found(
            f"Object '{guid}' is of type '{row['object_type']}', not '{object_type}'."
        )
    schema = get_object_schema(app, row["object_type"], required=True)
    full = _project_full(app, row, schema["fields"], row["object_type"])
    if include:
        _resolve_includes(app, full["_type"], [full], _parse_include(include))
    return full


def _classify_filter_key(key, fields, rel_views):
    """Split a filter key into ``(name, op, kind, view)``.

    ``kind`` is ``"field"`` (filtered against the JSON blob) or ``"relation"``
    (filtered through the indexed ``associations`` table, see
    :func:`_relation_clause`). A field name wins if a name is somehow both, which
    mirrors how writes resolve a key (see ``_split_body``).
    """
    if "__" in key:
        name, op = key.rsplit("__", 1)
        if op not in _OPS:
            # treat the whole thing as a name with default eq
            name, op = key, "eq"
    else:
        name, op = key, "eq"
    if name in fields:
        return name, op, "field", None
    if name in rel_views:
        return name, op, "relation", rel_views[name]
    raise bad_request(
        f"Cannot filter on unknown key '{name}'. "
        f"Declared fields: {sorted(fields)}; "
        f"relations: {sorted(rel_views)}. "
        "Filter a relation by a neighbor guid, e.g. ?<relation>=<guid>."
    )


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


# Operators meaningful on a relation (a set of neighbor guids) rather than a
# scalar field. A relation filter resolves through the indexed associations
# table; scalar comparisons (gt/lt/contains) are field-only.
_REL_OPS = {"eq", "ne", "in", "exists"}


def _relation_clause(app, view, op, raw):
    """A WHERE fragment that filters this type's objects by one relation.

    Emits a subquery over the **indexed** ``associations`` table, so a relation
    filter is index-backed (unlike a field filter, which scans the JSON blob)::

        ?assignee=<userguid>          tasks linked to that user        (eq)
        ?assignee__in=<g1>,<g2>       tasks linked to any of them      (in)
        ?assignee__ne=<userguid>      tasks NOT linked to that user    (ne)
        ?assignee__exists=true        tasks that have any assignee     (exists)

    ``view`` is one entry from :func:`associations.relation_views` (it carries
    the edge ``side`` and the synthetic ``assoc_name``). Returns
    ``(clause_sql, params)``.
    """
    if op not in _REL_OPS:
        raise bad_request(
            f"Operator '{op}' is not supported on relation '{view['key']}'. "
            f"Relations support {sorted(_REL_OPS)} — filter by a neighbor guid; "
            "comparisons like gt/lt/contains are field-only."
        )
    side, name = view["side"], view["assoc_name"]

    # Predicate on the column that holds the *neighbor* of a given edge, plus its
    # params. Returns (None, []) for an empty ``in`` list (matches no edge).
    def neighbor_pred(nb_col):
        if op == "exists":
            return "TRUE", []                            # any edge counts
        if op == "in":
            guids = raw.split(",") if isinstance(raw, str) else list(raw)
            guids = [str(g) for g in guids]
            if not guids:
                return None, []
            return f"{nb_col} IN ({','.join('?' * len(guids))})", guids
        return f"{nb_col} = ?", [str(raw)]               # eq / ne

    if side == "sym":
        # The object may sit on either end, so match the neighbor on both.
        p_to, a_to = neighbor_pred("to_guid")
        p_from, a_from = neighbor_pred("from_guid")
        if p_to is None:
            inner, iparams = None, []
        else:
            inner = (
                f"SELECT from_guid FROM associations "
                f"WHERE app=? AND assoc_name=? AND {p_to} "
                f"UNION SELECT to_guid FROM associations "
                f"WHERE app=? AND assoc_name=? AND {p_from}"
            )
            iparams = [app, name, *a_to, app, name, *a_from]
    else:
        my_col = "from_guid" if side == "from" else "to_guid"
        nb_col = "to_guid" if side == "from" else "from_guid"
        pred, pargs = neighbor_pred(nb_col)
        if pred is None:
            inner, iparams = None, []
        else:
            inner = (f"SELECT {my_col} FROM associations "
                     f"WHERE app=? AND assoc_name=? AND {pred}")
            iparams = [app, name, *pargs]

    if inner is None:                                    # empty __in list
        # eq/in match nothing; ne/exists-false match everything.
        return ("FALSE", []) if op in ("eq", "in") else ("TRUE", [])

    if op == "exists":
        from .fieldtypes import coerce_value
        negate = not coerce_value(view["key"], raw, "boolean")
    else:
        negate = op == "ne"
    return f"guid {'NOT IN' if negate else 'IN'} ({inner})", iparams


# Filter operator -> its SQL comparison, applied to the indexed value column.
# exists / in / contains are handled separately below.
_CMP_SQL = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _like_contains(default, val):
    """Python view of SQLite ``LIKE '%val%'`` (ASCII-case-insensitive substring),
    used only to decide whether default-valued rows join a ``contains`` filter."""
    try:
        return str(val).lower() in str(default).lower()
    except Exception:
        return False


def _absent_matches(default, op, val):
    """Would an object with no indexed value for this field satisfy ``op``/``val``?

    Such an object (field absent, or stale at the wrong type after a retype) is
    shown by the read path as the field's ``default`` (possibly None). It matches
    iff ``default <op> val`` does — mirroring read-time projection
    (:func:`fieldtypes.project_data`), so index filtering and reads stay in
    lockstep. ``default`` is a single known constant, evaluated here in Python.
    """
    d = default
    if op == "eq":
        return d == val
    if op == "ne":                       # null-safe: a None default "is not val"
        return d != val
    if op == "exists":
        return (d is not None) if val else (d is None)
    if op == "in":
        return d is not None and d in val
    if op == "contains":
        return d is not None and _like_contains(d, val)
    if d is None:                        # gt/gte/lt/lte against a null default
        return False
    try:
        if op == "gt":
            return d > val
        if op == "gte":
            return d >= val
        if op == "lt":
            return d < val
        if op == "lte":
            return d <= val
    except TypeError:
        return False
    return False


def _indexed_field_clause(app, object_type, field, fdef, op, val):
    """A WHERE fragment filtering this type's objects by one scalar field, via the
    indexed ``field_index`` table. Returns ``(clause_sql, params)``.

    Up to two parts, OR-ed:
      * present — objects whose indexed value matches the comparison (an index
        probe on field_index);
      * default — objects with *no* current-type value (absent, or stale after a
        retype), which the read path shows as the field's default; included only
        when the default itself satisfies the comparison (:func:`_absent_matches`).

    Together these reproduce read-time projection semantics exactly, while the
    common case (a real stored value) is served by the index.
    """
    col = fieldindex.column_for_type(fdef["type"])
    default = fdef.get("default")
    prefix = "app = ? AND object_type = ? AND field_name = ?"
    pre = [app, object_type, field]

    def present(pred_sql, pred_params):
        return (f"guid IN (SELECT object_id FROM field_index "
                f"WHERE {prefix} AND {pred_sql})", pre + pred_params)

    def no_value():
        # Objects with no non-null value in the current-type column -> read as
        # default (covers both absent fields and stale rows in the wrong column).
        return (f"guid NOT IN (SELECT object_id FROM field_index "
                f"WHERE {prefix} AND {col} IS NOT NULL)", list(pre))

    if op == "exists":
        if val:
            # "has an effective value." With a default, every object has one.
            if default is not None:
                return "TRUE", []
            return (f"guid IN (SELECT object_id FROM field_index "
                    f"WHERE {prefix} AND {col} IS NOT NULL)", list(pre))
        if default is not None:
            return "FALSE", []
        return no_value()

    if op == "in":
        if not val:                      # empty IN matches nothing
            return "FALSE", []
        qmarks = ",".join("?" * len(val))
        clause, params = present(f"{col} IN ({qmarks})", [_safe_bind(v) for v in val])
    elif op == "contains":
        esc = (str(val).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
        # SQLite LIKE is case-insensitive; Postgres needs ILIKE for the same
        # substring semantics (kept in lockstep with _like_contains below).
        clause, params = present(f"{col} {db.like_ci()} ? ESCAPE '\\'", [f"%{esc}%"])
    else:
        clause, params = present(f"{col} {_CMP_SQL[op]} ?", [_safe_bind(val)])

    if _absent_matches(default, op, val):
        nclause, nparams = no_value()
        return f"({clause} OR {nclause})", params + nparams
    return clause, params


def _build_where(app, object_type, filters, fields, rel_views):
    """Translate a ``{key: value}`` filter mapping into a SQL WHERE fragment.

    Field filters support operators via ``field__op`` keys: eq, ne, gt, gte, lt,
    lte, contains, in, exists. Relation filters (``rel``/``rel__op``) support eq,
    ne, in, exists and resolve through the indexed associations table.
    """
    clauses = []
    params = []
    for key, raw in filters.items():
        field, op, kind, view = _classify_filter_key(key, fields, rel_views)
        if kind == "relation":
            clause, p = _relation_clause(app, view, op, raw)
            clauses.append(clause)
            params.extend(p)
            continue
        fdef = fields[field]
        ftype = fdef["type"]
        if ftype == "json":
            raise bad_request(
                f"Field '{field}' is json and can't be filtered (json values "
                "aren't indexable). Filter on a scalar, indexed field instead.")
        if not fdef.get("index"):
            raise bad_request(
                f"Field '{field}' is not indexed, so it can't be filtered. Add "
                f"\"index\": true to '{field}' in its schema to filter or sort on it.")
        val = _coerce_filter_value(field, op, raw, ftype)
        clause, p = _indexed_field_clause(app, object_type, field, fdef, op, val)
        clauses.append(clause)
        params.extend(p)
    return clauses, params


def list_objects(app, object_type, filters=None, limit=DEFAULT_LIMIT, offset=0,
                 sort=None, order="asc", include=None):
    schema = get_object_schema(app, object_type, required=True)
    fields = schema["fields"]
    filters = filters or {}
    rel_views = {v["key"]: v for v in assoc.relation_views(app, object_type)}

    clauses = ["app = ?", "object_type = ?"]
    params = [app, object_type]
    fc, fp = _build_where(app, object_type, filters, fields, rel_views)
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
            fsort = fields[sort]
            if fsort["type"] == "json":
                raise bad_request(f"Cannot sort on json field '{sort}'.")
            if not fsort.get("index"):
                raise bad_request(
                    f"Field '{sort}' is not indexed, so it can't be sorted on. Add "
                    f"\"index\": true to '{sort}' in its schema to sort on it.")
            # Order by the indexed value (PK-backed correlated lookup), falling
            # back to the field's default for objects with no current-type value,
            # so the order matches what reads project — same rule as filters.
            col = fieldindex.column_for_type(fsort["type"])
            sub = (f"(SELECT {col} FROM field_index "
                   f"WHERE object_id = objects.guid AND field_name = ?)")
            sort_params.append(sort)
            default = fsort.get("default")
            if default is not None:
                sort_expr = f"COALESCE({sub}, ?)"
                sort_params.append(_safe_bind(default))
            else:
                sort_expr = sub
            order_clause = f"{sort_expr} {order_sql}, guid ASC"
        else:
            raise bad_request(
                f"Cannot sort on '{sort}'. Sort by a declared field, "
                "_created_at, _updated_at, or _guid. (Relations and json fields "
                "aren't sortable; filter relations instead, e.g. ?rel=<guid>.)")
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

    projected = [_project_fields(r, fields) for r in rows]
    relmap = assoc.project_relations(app, [r["guid"] for r in rows], object_type)
    for p in projected:
        p.update(relmap[p["_guid"]])

    if include:
        _resolve_includes(app, object_type, projected, _parse_include(include))

    return {
        "objects": projected,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --- nested includes (graph-shaped reads) -------------------------------------

MAX_INCLUDE_DEPTH = 4


def _parse_include(include):
    """Parse an ``include`` spec into a nested tree of relation names.

    Accepts a comma-separated string of dotted paths (``"comments,comments.author"``)
    or a list of paths. ``comments.author`` means: include each comment, and within
    each comment include its author. Returns e.g. ``{"comments": {"author": {}}}``.
    Paths deeper than :data:`MAX_INCLUDE_DEPTH` relations are rejected.
    """
    if not include:
        return {}
    paths = include.split(",") if isinstance(include, str) else list(include)
    tree = {}
    for path in paths:
        path = path.strip()
        if not path:
            continue
        parts = [p.strip() for p in path.split(".")]
        if any(not p for p in parts):
            raise bad_request(f"Malformed include path '{path}'.")
        if len(parts) > MAX_INCLUDE_DEPTH:
            raise bad_request(
                f"include path '{path}' is too deep (max {MAX_INCLUDE_DEPTH} relations).")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
    return tree


def _fetch_projected(app, object_type, guids):
    """Fetch + project objects of one type by guid into ``{guid: body}``.

    One batched query (chunked under SQLite's bind-variable limit) plus one batched
    relation projection — no per-object fan-out. Missing guids (e.g. a since-deleted
    neighbor) are simply absent from the result.
    """
    seen = list(dict.fromkeys(g for g in guids if g))
    if not seen:
        return {}
    fields = get_object_schema(app, object_type, required=True)["fields"]
    rows = []
    c = db.conn()
    CHUNK = 400
    for i in range(0, len(seen), CHUNK):
        part = seen[i:i + CHUNK]
        qmarks = ",".join("?" * len(part))
        rows.extend(c.execute(
            f"SELECT * FROM objects WHERE app=? AND object_type=? AND guid IN ({qmarks})",
            [app, object_type, *part]).fetchall())
    projected = [_project_fields(r, fields) for r in rows]
    relmap = assoc.project_relations(app, [r["guid"] for r in rows], object_type)
    out = {}
    for p in projected:
        p.update(relmap[p["_guid"]])
        out[p["_guid"]] = p
    return out


def _resolve_includes(app, object_type, bodies, include_tree):
    """Hydrate relation values on ``bodies`` in place, per ``include_tree``.

    For each included relation, the guid(s) it holds are replaced by the full
    neighbor object(s) (nested, Prisma-style); deeper levels recurse. Resolved
    breadth-first and batched: one fetch per relation per level, so a page of N
    objects with an included relation is a handful of queries, not N+1.
    """
    if not include_tree or not bodies:
        return
    rel_views = {v["key"]: v for v in assoc.relation_views(app, object_type)}
    for key, subtree in include_tree.items():
        view = rel_views.get(key)
        if view is None:
            raise bad_request(
                f"Cannot include '{key}' on type '{object_type}': it is not a "
                f"relation. Relations: {sorted(rel_views)}.")
        neighbor_type = view["neighbor_type"]
        guids = []
        for body in bodies:
            val = body.get(key)
            if isinstance(val, list):
                guids.extend(val)
            elif val is not None:
                guids.append(val)
        fetched = _fetch_projected(app, neighbor_type, guids)
        if subtree:
            _resolve_includes(app, neighbor_type, list(fetched.values()), subtree)
        for body in bodies:
            val = body.get(key)
            if isinstance(val, list):
                body[key] = [fetched[g] for g in val if g in fetched]
            elif val is not None:
                body[key] = fetched.get(val)
