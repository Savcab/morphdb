"""Relationships between objects.

Two layers, mirroring object schemas vs. objects:

* **Association schema** (the type): a named relationship with a from-type, a
  to-type, a label for each direction, and a cardinality. Defined by the agent.
* **Association** (the instance): one edge connecting two concrete guids. Stored
  as a single canonical row; traversal is bidirectional by querying both ends.

From the perspective of object X on an edge:
    - if X is the ``from`` end, the neighbor plays the ``forward_name`` role;
    - if X is the ``to`` end, the neighbor plays the ``inverse_name`` role.

Example: type ``parentage`` from=parent to=child, forward_name="child",
inverse_name="parent". Asking a parent for relation "child" yields their kids;
asking a child for relation "parent" yields the parent.
"""

import json

from . import db
from .errors import ApiError, bad_request, conflict, not_found
from .schema import _validate_type_name, get_object_schema
from .util import now_iso

CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}


def _row_to_assoc_schema(row):
    return {
        "name": row["name"],
        "from_type": row["from_type"],
        "to_type": row["to_type"],
        "forward_name": row["forward_name"],
        "inverse_name": row["inverse_name"],
        "cardinality": row["cardinality"],
        "symmetric": bool(row["symmetric"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --- association schemas ------------------------------------------------------


def upsert_association_schema(name, from_type, to_type, forward_name,
                              inverse_name, cardinality, symmetric=False):
    _validate_type_name(name)
    if cardinality not in CARDINALITIES:
        raise bad_request(
            f"Unknown cardinality '{cardinality}'. One of {sorted(CARDINALITIES)}."
        )
    # Robust boolean parse: bool("false") is truthy in Python, so don't use it.
    if isinstance(symmetric, str):
        symmetric = symmetric.strip().lower() in ("true", "1", "yes", "on")
    else:
        symmetric = bool(symmetric)
    if symmetric:
        # A symmetric relationship is mutual (friends, peers): the edge A–B is
        # the same as B–A. It only makes sense within one type, and a single
        # label describes both ends.
        if from_type != to_type:
            raise bad_request(
                "symmetric associations require from_type == to_type."
            )
        if cardinality not in ("one_to_one", "many_to_many"):
            raise bad_request(
                "symmetric associations must be one_to_one or many_to_many."
            )
        # A symmetric edge has a single traversal label; force inverse_name to
        # match forward_name so there is no dead label that matches nothing.
        inverse_name = forward_name
    for label, val in (("forward_name", forward_name), ("inverse_name", inverse_name)):
        if not isinstance(val, str) or not val.strip():
            raise bad_request(f"'{label}' must be a non-empty string.")

    # Both endpoint types must exist so traversal/validation is meaningful.
    get_object_schema(from_type, required=True)
    get_object_schema(to_type, required=True)

    ts = now_iso()
    with db.transaction() as c:
        existing = c.execute(
            "SELECT name FROM association_schemas WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE association_schemas SET from_type=?, to_type=?, "
                "forward_name=?, inverse_name=?, cardinality=?, symmetric=?, "
                "updated_at=? WHERE name=?",
                (from_type, to_type, forward_name, inverse_name, cardinality,
                 int(symmetric), ts, name),
            )
        else:
            c.execute(
                "INSERT INTO association_schemas (name, from_type, to_type, "
                "forward_name, inverse_name, cardinality, symmetric, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, from_type, to_type, forward_name, inverse_name,
                 cardinality, int(symmetric), ts, ts),
            )
        if symmetric:
            _canonicalize_symmetric_edges(c, name)
        _check_existing_cardinality(c, name, cardinality, symmetric)
        row = c.execute(
            "SELECT * FROM association_schemas WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_assoc_schema(row)


def _check_existing_cardinality(c, name, cardinality, symmetric):
    """Reject a schema change whose new cardinality the existing edges violate.

    Cardinality must mean something; silently keeping over-capacity edges would
    break the invariant. The transaction rolls back on conflict, so the schema
    change is atomic.
    """
    def first_over_capacity(column):
        return c.execute(
            f"SELECT {column} FROM associations WHERE assoc_name=? "
            f"GROUP BY {column} HAVING COUNT(*) > 1 LIMIT 1",
            (name,),
        ).fetchone()

    offender = None
    if symmetric:
        if cardinality == "one_to_one":
            offender = c.execute(
                "SELECT g FROM (SELECT from_guid AS g FROM associations "
                "WHERE assoc_name=? UNION ALL SELECT to_guid FROM associations "
                "WHERE assoc_name=?) GROUP BY g HAVING COUNT(*) > 1 LIMIT 1",
                (name, name),
            ).fetchone()
    else:
        if cardinality in ("one_to_one", "many_to_one"):
            offender = first_over_capacity("from_guid")
        if offender is None and cardinality in ("one_to_one", "one_to_many"):
            offender = first_over_capacity("to_guid")

    if offender is not None:
        raise conflict(
            f"Existing edges of '{name}' violate cardinality '{cardinality}'. "
            "Remove the conflicting edges before changing the cardinality."
        )


def _canonicalize_symmetric_edges(c, name):
    """Rewrite existing edges of a now-symmetric association into canonical
    (sorted) order and drop reverse duplicates. One-time cost on the schema
    edit; keeps the "counted once / deduped either order" invariant for edges
    created before the type was flipped to symmetric.
    """
    rows = c.execute(
        "SELECT from_guid, to_guid, created_at FROM associations WHERE assoc_name=?",
        (name,),
    ).fetchall()
    canon = {}
    for r in rows:
        a, b = sorted((r["from_guid"], r["to_guid"]))
        key = (a, b)
        prev = canon.get(key)
        if prev is None or r["created_at"] < prev:
            canon[key] = r["created_at"]
    c.execute("DELETE FROM associations WHERE assoc_name=?", (name,))
    for (a, b), created in canon.items():
        c.execute(
            "INSERT INTO associations (assoc_name, from_guid, to_guid, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, a, b, created),
        )


def get_association_schema(name, required=False):
    row = db.conn().execute(
        "SELECT * FROM association_schemas WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        if required:
            raise not_found(f"No association type named '{name}'. Define it first.")
        return None
    return _row_to_assoc_schema(row)


def list_association_schemas():
    rows = db.conn().execute(
        "SELECT * FROM association_schemas ORDER BY name"
    ).fetchall()
    return [_row_to_assoc_schema(r) for r in rows]


def delete_association_schema(name, cascade=True):
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM association_schemas WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise not_found(f"No association type named '{name}'.")
        n = c.execute(
            "SELECT COUNT(*) AS n FROM associations WHERE assoc_name = ?", (name,)
        ).fetchone()["n"]
        if n and not cascade:
            raise bad_request(
                f"Association type '{name}' still has {n} edge(s). "
                "Pass cascade=true to delete them too."
            )
        c.execute("DELETE FROM associations WHERE assoc_name = ?", (name,))
        c.execute("DELETE FROM association_schemas WHERE name = ?", (name,))
    return {"deleted": name, "edges_removed": n}


# --- association instances ----------------------------------------------------


def _require_object(c, guid):
    row = c.execute(
        "SELECT guid, object_type FROM objects WHERE guid = ?", (guid,)
    ).fetchone()
    if row is None:
        raise not_found(f"No object with guid '{guid}'.")
    return row


def create_association(name, from_guid, to_guid, replace=False):
    """Create an edge ``from_guid --name--> to_guid``, enforcing cardinality.

    Idempotent: re-creating the exact same edge returns the existing one.
    On a cardinality conflict, either raise 409 or, with ``replace=True``,
    drop the conflicting edge(s) first.
    """
    if from_guid == to_guid:
        raise bad_request("Self-referential edges (from == to) are not allowed.")

    schema = get_association_schema(name, required=True)
    symmetric = schema["symmetric"]
    ts = now_iso()

    with db.transaction() as c:
        frow = _require_object(c, from_guid)
        trow = _require_object(c, to_guid)

        if frow["object_type"] != schema["from_type"]:
            raise bad_request(
                f"from_guid is a '{frow['object_type']}' but association '{name}' "
                f"expects from_type '{schema['from_type']}'."
            )
        if trow["object_type"] != schema["to_type"]:
            raise bad_request(
                f"to_guid is a '{trow['object_type']}' but association '{name}' "
                f"expects to_type '{schema['to_type']}'."
            )

        # For symmetric relationships, A–B and B–A are the same edge: store the
        # endpoints in a canonical (sorted) order so it is deduped naturally.
        if symmetric and from_guid > to_guid:
            from_guid, to_guid = to_guid, from_guid

        # Already exists? Idempotent return.
        exact = c.execute(
            "SELECT * FROM associations WHERE assoc_name=? AND from_guid=? AND to_guid=?",
            (name, from_guid, to_guid),
        ).fetchone()
        if exact:
            return _edge_dict(exact, schema)

        # Cardinality conflicts: collect edges that would violate the rule.
        card = schema["cardinality"]
        conflicts = []
        if symmetric:
            # A node's degree counts edges where it appears at either end.
            if card == "one_to_one":
                for node in (from_guid, to_guid):
                    conflicts += c.execute(
                        "SELECT * FROM associations WHERE assoc_name=? AND "
                        "(from_guid=? OR to_guid=?)",
                        (name, node, node),
                    ).fetchall()
            # many_to_many: no limit.
        else:
            if card in ("one_to_one", "many_to_one"):
                # the `from` side may have at most one `to`
                conflicts += c.execute(
                    "SELECT * FROM associations WHERE assoc_name=? AND from_guid=?",
                    (name, from_guid),
                ).fetchall()
            if card in ("one_to_one", "one_to_many"):
                # the `to` side may have at most one `from`
                conflicts += c.execute(
                    "SELECT * FROM associations WHERE assoc_name=? AND to_guid=?",
                    (name, to_guid),
                ).fetchall()

        # De-duplicate conflict rows by id (symmetric one_to_one can match twice).
        if conflicts:
            seen_ids = set()
            uniq = []
            for r in conflicts:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    uniq.append(r)
            conflicts = uniq
            if not replace:
                pretty = [
                    {"from_guid": r["from_guid"], "to_guid": r["to_guid"]}
                    for r in conflicts
                ]
                raise conflict(
                    f"Creating this edge would violate '{card}' on association "
                    f"'{name}'. Conflicting edge(s) exist. Pass replace=true to "
                    "overwrite, or delete them first.",
                    conflicts=pretty,
                )
            ids = [r["id"] for r in conflicts]
            qmarks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM associations WHERE id IN ({qmarks})", ids)

        c.execute(
            "INSERT INTO associations (assoc_name, from_guid, to_guid, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, from_guid, to_guid, ts),
        )
        row = c.execute(
            "SELECT * FROM associations WHERE assoc_name=? AND from_guid=? AND to_guid=?",
            (name, from_guid, to_guid),
        ).fetchone()
    return _edge_dict(row, schema)


def delete_association(name, from_guid, to_guid):
    schema = get_association_schema(name, required=True)
    if schema["symmetric"] and from_guid > to_guid:
        from_guid, to_guid = to_guid, from_guid
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM associations WHERE assoc_name=? AND from_guid=? AND to_guid=?",
            (name, from_guid, to_guid),
        ).fetchone()
        if row is None:
            raise not_found(
                f"No '{name}' edge from '{from_guid}' to '{to_guid}'."
            )
        c.execute("DELETE FROM associations WHERE id = ?", (row["id"],))
    return {"deleted": {"assoc_name": name, "from_guid": from_guid, "to_guid": to_guid}}


def _edge_dict(row, schema):
    return {
        "assoc_name": row["assoc_name"],
        "from_guid": row["from_guid"],
        "to_guid": row["to_guid"],
        "forward_name": schema["forward_name"],
        "inverse_name": schema["inverse_name"],
        "cardinality": schema["cardinality"],
        "symmetric": schema["symmetric"],
        "created_at": row["created_at"],
    }


def get_associations(guid, name=None, relation=None, direction=None, expand=False):
    """Return all edges touching ``guid``, each described from ``guid``'s view.

    Filters:
      name      — only this association type
      relation  — only edges where the neighbor plays this role for ``guid``
      direction — "forward"/"outgoing" (guid is from) or "inverse"/"incoming"
    """
    c = db.conn()
    # Confirm the object exists for a clean error.
    if c.execute("SELECT 1 FROM objects WHERE guid = ?", (guid,)).fetchone() is None:
        raise not_found(f"No object with guid '{guid}'.")

    if direction not in (None, "", "forward", "outgoing", "inverse",
                         "incoming", "symmetric"):
        raise bad_request(
            f"Unknown direction '{direction}'. Use 'forward' or 'inverse'."
        )

    # Validate filters so a typo'd name/relation errors instead of silently
    # returning an empty list (consistent with field-filter validation).
    if name and get_association_schema(name) is None:
        raise not_found(f"No association type named '{name}'.")
    if relation is not None:
        valid = set()
        for s in list_association_schemas():
            valid.add(s["forward_name"])
            valid.add(s["inverse_name"])
        if relation not in valid:
            raise bad_request(
                f"Unknown relation '{relation}'. Known relations: {sorted(valid)}."
            )

    sql = (
        "SELECT a.*, s.from_type, s.to_type, s.forward_name, s.inverse_name, "
        "s.cardinality, s.symmetric, "
        "fo.object_type AS from_obj_type, too.object_type AS to_obj_type "
        "FROM associations a "
        "JOIN association_schemas s ON a.assoc_name = s.name "
        "LEFT JOIN objects fo ON fo.guid = a.from_guid "
        "LEFT JOIN objects too ON too.guid = a.to_guid "
        "WHERE (a.from_guid = ? OR a.to_guid = ?)"
    )
    params = [guid, guid]
    if name:
        sql += " AND a.assoc_name = ?"
        params.append(name)
    sql += " ORDER BY a.created_at, a.id"

    dirn = (direction or "").lower()
    out = []
    for row in c.execute(sql, params).fetchall():
        is_from = row["from_guid"] == guid
        is_symmetric = bool(row["symmetric"])

        if is_symmetric:
            # Mutual edge: one label, neighbor is the other endpoint. It has no
            # forward/inverse direction, so only direction=symmetric keeps it.
            if dirn in ("forward", "outgoing", "inverse", "incoming"):
                continue
            rel = row["forward_name"]
            neighbor_guid = row["to_guid"] if is_from else row["from_guid"]
            # Derive neighbor type from the actual object (falls back to the
            # schema type if the object is missing), so it stays correct even if
            # the schema's from_type/to_type were later changed.
            neighbor_type = ((row["to_obj_type"] if is_from else row["from_obj_type"])
                             or row["to_type"])
            this_direction = "symmetric"
        else:
            # Directional edge: excluded when only symmetric edges are requested.
            if dirn == "symmetric":
                continue
            if dirn in ("forward", "outgoing") and not is_from:
                continue
            if dirn in ("inverse", "incoming") and is_from:
                continue
            if is_from:
                rel = row["forward_name"]
                neighbor_guid = row["to_guid"]
                neighbor_type = row["to_obj_type"] or row["to_type"]
                this_direction = "forward"
            else:
                rel = row["inverse_name"]
                neighbor_guid = row["from_guid"]
                neighbor_type = row["from_obj_type"] or row["from_type"]
                this_direction = "inverse"

        if relation and rel != relation:
            continue

        item = {
            "assoc_name": row["assoc_name"],
            "relation": rel,
            "direction": this_direction,
            "neighbor_guid": neighbor_guid,
            "neighbor_type": neighbor_type,
            "cardinality": row["cardinality"],
            "symmetric": is_symmetric,
            "created_at": row["created_at"],
        }
        if expand:
            from .objects import get_object

            # Only a missing neighbor (404) becomes null; other errors surface.
            try:
                item["neighbor"] = get_object(neighbor_guid)
            except ApiError as e:
                if e.status == 404:
                    item["neighbor"] = None
                else:
                    raise
        out.append(item)

    return {"guid": guid, "associations": out, "total": len(out)}
