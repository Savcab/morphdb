"""Object schema management — the part a coding agent edits constantly.

A schema is ``{name, fields, created_at, updated_at}`` where ``fields`` maps a
field name to its normalized definition. Editing a schema is O(1): we never
touch stored objects. Reads reinterpret old rows through the current schema
(lazy invalidation), so adding/removing fields is instant regardless of how
many objects exist.
"""

import json
import re

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


def _row_to_schema(row):
    return {
        "name": row["name"],
        "fields": json.loads(row["fields"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_object_schema(name, fields, merge=False):
    """Create or replace an object type's schema.

    By default the provided ``fields`` fully define the type (replace semantics).
    With ``merge=True`` the given fields are merged into any existing definition,
    which is handy when an agent wants to add a column without restating the rest.
    """
    _validate_type_name(name)
    new_fields = normalize_fields(fields)
    ts = now_iso()

    with db.transaction() as c:
        existing = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()

        old_fields = json.loads(existing["fields"]) if existing else {}
        if existing and merge:
            merged = dict(old_fields)
            merged.update(new_fields)
            new_fields = merged

        if existing:
            c.execute(
                "UPDATE object_schemas SET fields = ?, updated_at = ? WHERE name = ?",
                (json.dumps(new_fields), ts, name),
            )
            # If any field's *type* changed, re-coerce existing stored values to
            # the new type (uncoercible -> dropped). Adding/removing fields stays
            # O(1) and never touches rows; only a genuine type change migrates
            # data, keeping stored values consistent with the schema so reads and
            # queries agree.
            retyped = [
                f for f, d in new_fields.items()
                if f in old_fields and old_fields[f]["type"] != d["type"]
            ]
            if retyped:
                _migrate_retyped_fields(c, name, new_fields, retyped, ts)
        else:
            c.execute(
                "INSERT INTO object_schemas (name, fields, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, json.dumps(new_fields), ts, ts),
            )

        row = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_schema(row)


def _migrate_retyped_fields(c, name, fields, retyped, ts):
    """Re-coerce the given fields' stored values to their new types in place."""
    from .fieldtypes import coerce_value

    rows = c.execute(
        "SELECT guid, data FROM objects WHERE object_type = ?", (name,)
    ).fetchall()
    for r in rows:
        blob = json.loads(r["data"])
        changed = False
        for f in retyped:
            if f not in blob or blob[f] is None:
                continue
            try:
                new_val = coerce_value(f, blob[f], fields[f]["type"])
            except Exception:
                new_val = None  # uncoercible -> drop so it can't read/query wrong
            if new_val is None:
                del blob[f]
            else:
                blob[f] = new_val
            changed = True
        if changed:
            c.execute(
                "UPDATE objects SET data = ?, updated_at = ? WHERE guid = ?",
                (json.dumps(blob), ts, r["guid"]),
            )


def get_object_schema(name, required=False):
    row = db.conn().execute(
        "SELECT * FROM object_schemas WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        if required:
            raise not_found(f"No object type named '{name}'. Define it first.")
        return None
    return _row_to_schema(row)


def list_object_schemas():
    rows = db.conn().execute(
        "SELECT * FROM object_schemas ORDER BY name"
    ).fetchall()
    return [_row_to_schema(r) for r in rows]


def delete_object_fields(name, field_names):
    """Remove fields from a schema. Stored data is left untouched (lazy).

    The removed fields simply stop appearing on read.
    """
    if not isinstance(field_names, (list, tuple)) or not field_names:
        raise bad_request("Provide a non-empty list of field names to delete.")
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise not_found(f"No object type named '{name}'.")
        fields = json.loads(row["fields"])
        missing = [f for f in field_names if f not in fields]
        if missing:
            raise bad_request(f"Field(s) {missing} are not in schema '{name}'.")
        for f in field_names:
            del fields[f]
        c.execute(
            "UPDATE object_schemas SET fields = ?, updated_at = ? WHERE name = ?",
            (json.dumps(fields), now_iso(), name),
        )
        row = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_schema(row)


def delete_object_schema(name, cascade=True):
    """Delete an object type. With ``cascade`` (default) also remove its objects
    and any associations touching them; otherwise refuse if objects exist.
    """
    with db.transaction() as c:
        row = c.execute(
            "SELECT * FROM object_schemas WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise not_found(f"No object type named '{name}'.")

        guids = [
            r["guid"]
            for r in c.execute(
                "SELECT guid FROM objects WHERE object_type = ?", (name,)
            ).fetchall()
        ]

        if guids and not cascade:
            raise bad_request(
                f"Object type '{name}' still has {len(guids)} object(s). "
                "Pass cascade=true to delete them too."
            )

        if guids:
            qmarks = ",".join("?" * len(guids))
            c.execute(f"DELETE FROM objects WHERE guid IN ({qmarks})", guids)
            c.execute(
                f"DELETE FROM associations WHERE from_guid IN ({qmarks}) "
                f"OR to_guid IN ({qmarks})",
                guids + guids,
            )

        # Also drop association schemas that reference this type — they are no
        # longer satisfiable.
        c.execute(
            "DELETE FROM association_schemas WHERE from_type = ? OR to_type = ?",
            (name, name),
        )
        c.execute("DELETE FROM object_schemas WHERE name = ?", (name,))

    return {"deleted": name, "objects_removed": len(guids)}
