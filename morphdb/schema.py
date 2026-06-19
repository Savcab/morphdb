"""Object type schemas — the thing a coding agent edits constantly.

A *type* bundles raw ``fields`` and ``relations`` (links to other types) into a
single schema document. Editing it is O(1): we never rewrite stored objects.
Reads reinterpret old rows through the current schema (lazy invalidation), so
adding/removing/retyping a field is instant regardless of object count.

Relations are stored in their own table (see :mod:`associations`) because they
have cardinality and two ends, but from the agent's point of view they live
right inside the type document alongside fields — declared once, on one side.
"""

import json
import re

from . import associations as assoc
from . import db
from .errors import bad_request, not_found
from .fieldtypes import normalize_fields
from .util import now_iso

# \Z (not $) so a trailing newline cannot sneak through (e.g. "task\n").
_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")


def _validate_type_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise bad_request(
            f"Invalid object type name {name!r}. Use a letter followed by "
            "letters, digits, or underscores (e.g. 'task', 'blog_post')."
        )
    return name


# --- low-level fields access (used internally by objects/associations) --------


def get_object_schema(name, required=False):
    """Return the raw ``{name, fields, created_at, updated_at}`` for a type.

    Just the stored fields map — relations are resolved separately. This is the
    hot path for object reads/writes.
    """
    row = db.conn().execute(
        "SELECT * FROM object_schemas WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        if required:
            raise not_found(f"No object type named '{name}'. Define it first.")
        return None
    return {
        "name": row["name"],
        "fields": json.loads(row["fields"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --- full type documents (the agent-facing schema surface) --------------------


def get_type_doc(name, required=False):
    """The full schema document for one type: fields + relations + inverses."""
    base = get_object_schema(name, required=required)
    if base is None:
        return None
    relations, inverse = assoc.schema_relations(name)
    return {
        "name": base["name"],
        "fields": base["fields"],
        "relations": relations,
        "inverse_relations": inverse,
        "created_at": base["created_at"],
        "updated_at": base["updated_at"],
    }


def list_type_docs():
    rows = db.conn().execute(
        "SELECT name FROM object_schemas ORDER BY name"
    ).fetchall()
    return [get_type_doc(r["name"], required=True) for r in rows]


def upsert_type(name, fields=None, relations=None, merge=False):
    """Create or update a type from a schema document.

    ``fields`` / ``relations`` that are ``None`` (absent from the request) are
    left untouched, so a partial edit is natural. When present:

      * ``merge=True``  — add/update the given fields/relations, drop nothing.
      * ``merge=False`` — replace: fields become exactly the given map; relations
        authored on this type become exactly the given set (omitted ones, and
        their edges, are removed). Inverse relations (authored elsewhere) are
        never affected.
    """
    _validate_type_name(name)
    if relations is not None and not isinstance(relations, dict):
        raise bad_request("'relations' must be an object mapping name -> definition.")

    with db.transaction() as c:
        existing = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
        ts = now_iso()

        # --- fields ---
        if fields is not None:
            new_fields = normalize_fields(fields)
            if existing and merge:
                final = dict(json.loads(existing["fields"]))
                final.update(new_fields)
            else:
                final = new_fields
        else:
            final = json.loads(existing["fields"]) if existing else {}

        if existing:
            c.execute(
                "UPDATE object_schemas SET fields = ?, updated_at = ? WHERE name = ?",
                (json.dumps(final), ts, name),
            )
        else:
            c.execute(
                "INSERT INTO object_schemas (name, fields, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, json.dumps(final), ts, ts),
            )

        # --- relations ---
        touched = {name}
        if relations is not None:
            for key, raw in relations.items():
                d = assoc.upsert_relation(c, name, key, raw)
                touched.add(d["to_type"])
            if not merge:
                assoc.prune_forward_relations(c, name, set(relations.keys()))

        # Field names and relation names share the object body namespace, so they
        # must not collide on any affected type.
        for t in touched:
            _assert_no_collisions(c, t)

    return get_type_doc(name, required=True)


def _assert_no_collisions(c, type_name):
    row = c.execute(
        "SELECT fields FROM object_schemas WHERE name = ?", (type_name,)
    ).fetchone()
    if row is None:
        return
    field_keys = set(json.loads(row["fields"]).keys())
    seen = set()
    for v in assoc.relation_views(type_name, c):
        k = v["key"]
        if k in field_keys:
            raise bad_request(
                f"Relation '{k}' on type '{type_name}' collides with a field of the "
                "same name. Rename one of them."
            )
        if k in seen:
            raise bad_request(
                f"Type '{type_name}' ends up with two relations named '{k}'. "
                "Give the inverse a distinct name."
            )
        seen.add(k)


def delete_type(name):
    """Delete a type, its own objects, and every edge touching those objects.

    Neighbor objects of *other* types are never deleted — only the relationship
    metadata and the edge rows go. Relations where this type was an endpoint are
    removed from the other types' schemas too.
    """
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise not_found(f"No object type named '{name}'.")

        guids = [
            r["guid"] for r in c.execute(
                "SELECT guid FROM objects WHERE object_type = ?", (name,)
            ).fetchall()
        ]
        if guids:
            qmarks = ",".join("?" * len(guids))
            c.execute(f"DELETE FROM objects WHERE guid IN ({qmarks})", guids)

        # Drop relationships (and their edges) where this type is an endpoint;
        # this also clears any edges from this type's objects to neighbors.
        assoc.delete_relations_touching_type(c, name)
        c.execute("DELETE FROM object_schemas WHERE name = ?", (name,))

    return {"deleted": name, "objects_removed": len(guids)}
