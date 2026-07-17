"""Relationships between objects — exposed as relation *fields* on a type.

A relationship is declared inside an object type's schema, under ``relations``::

    "assignee": {"to": "user", "cardinality": "many_to_one", "inverse": "tasks"}

Declared once (on the ``from`` side), it automatically shows up on the other
type as the inverse relation (``user.tasks``). The frontend then reads and
writes relations exactly like ordinary fields: a relation value is a neighbor
guid (to-one) or a list of neighbor guids (to-many), right inside the object
body. Behind the scenes each relationship is still its own type in the
``association_schemas`` table and each edge is a single canonical row in
``associations`` — single-row storage keeps bidirectional traversal consistent
with no dual-write hazard.

Everything here is scoped to one **app**: every query carries the app key, so
relationships and edges never cross tenant boundaries (a relation target must be
an object in the same app).

Multiplicity, from a given object's point of view:
    cardinality "X_to_Y"  →  the from-side sees Y neighbors, the to-side sees X.
So ``many_to_one`` (many tasks → one user): a task's ``assignee`` is one guid;
a user's ``tasks`` is a list.
"""

from . import db
from .errors import bad_request
from .fieldtypes import validate_member_name
from .util import now_iso

CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}


def _assoc_name(from_type, forward_name):
    """Internal name for a relationship: ``{from_type}__{forward_name}``.

    Unique within an app (paired with the app key in the table's primary key);
    never exposed in the API — the agent addresses relations by their field name
    on a type, not by this synthetic id.
    """
    return f"{from_type}__{forward_name}"


def _mult(cardinality, side):
    """Multiplicity ('one'/'many') seen from one end of a relationship.

    side='from' → the to-part of the cardinality; side='to' → the from-part.
    """
    frm, to = cardinality.split("_to_")
    return to if side == "from" else frm


# --- declaring relations (called by schema.py inside a transaction) -----------


def _parse_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


def normalize_relation_def(from_type, key, raw):
    """Validate one ``relations`` entry and return a normalized assoc-schema dict.

    ``key`` is the relation's name on ``from_type`` (its forward_name).
    """
    validate_member_name(key, "relation")
    if not isinstance(raw, dict):
        raise bad_request(
            f"Relation '{key}' must be an object like "
            "{\"to\": \"user\", \"cardinality\": \"many_to_one\", \"inverse\": \"tasks\"}."
        )
    to_type = raw.get("to")
    if not to_type or not isinstance(to_type, str):
        raise bad_request(f"Relation '{key}' needs a 'to' object type.")
    cardinality = raw.get("cardinality")
    if cardinality not in CARDINALITIES:
        raise bad_request(
            f"Relation '{key}' has unknown cardinality {cardinality!r}. "
            f"One of {sorted(CARDINALITIES)}."
        )
    symmetric = _parse_bool(raw.get("symmetric", False))
    fdesc = raw.get("description")
    idesc = raw.get("inverse_description")

    if symmetric:
        # A symmetric relationship is mutual (friends, peers): edge A–B == B–A.
        # Only meaningful within one type, with a single shared label.
        if to_type != from_type:
            raise bad_request(
                f"Relation '{key}' is symmetric, so 'to' must be '{from_type}'."
            )
        if cardinality not in ("one_to_one", "many_to_many"):
            raise bad_request(
                f"Symmetric relation '{key}' must be one_to_one or many_to_many."
            )
        inverse = key                      # one label describes both ends
        idesc = fdesc
    else:
        inverse = raw.get("inverse")
        if not inverse or not isinstance(inverse, str):
            raise bad_request(
                f"Relation '{key}' needs an 'inverse' name (the label the other "
                "side sees), e.g. \"inverse\": \"tasks\"."
            )
        validate_member_name(inverse, "relation")
        if from_type == to_type and inverse == key:
            raise bad_request(
                f"Relation '{key}' is a self-relation with identical forward and "
                "inverse names; either give the inverse a different name or set "
                "\"symmetric\": true."
            )

    return {
        "name": _assoc_name(from_type, key),
        "from_type": from_type,
        "to_type": to_type,
        "forward_name": key,
        "inverse_name": inverse,
        "cardinality": cardinality,
        "symmetric": symmetric,
        "forward_description": fdesc,
        "inverse_description": idesc,
    }


# Columns that define a relationship's meaning; a re-PUT that changes none of
# them is idempotent and must not count as a schema change (§5.6 of the
# streaming spec: boot-pattern re-PUTs reset nothing).
_DEF_COLS = ("from_type", "to_type", "forward_name", "inverse_name",
             "cardinality", "forward_description", "inverse_description")


def upsert_relation(c, app, from_type, key, raw):
    """Create or update one relationship declared on ``from_type`` in ``app``.

    Runs inside the caller's transaction cursor ``c``. The ``to`` type must
    already exist in the same app. Symmetric edges are canonicalized if the flag
    is (re)set. Returns ``(definition, changed, old_endpoints)`` — ``changed`` is
    False for an idempotent re-PUT; ``old_endpoints`` names the prior from/to
    types so a re-pointed relation resets the old neighbor's streams too (§5.6).
    """
    d = normalize_relation_def(from_type, key, raw)
    # The neighbor type must exist (in this app) so traversal/validation works.
    if c.get_object_schema(app, d["to_type"]) is None:
        raise bad_request(
            f"Relation '{key}' points to unknown type '{d['to_type']}'. Define it first."
        )
    ts = now_iso()
    existing = c.get_association_schema(app, d["name"])
    changed = (
        existing is None
        or bool(existing["symmetric"]) != d["symmetric"]
        or any(existing[k] != d[k] for k in _DEF_COLS)
    )
    old_endpoints = (set() if existing is None
                     else {existing["from_type"], existing["to_type"]})
    c.put_association_schema(app, d, ts, existing is not None)
    if d["symmetric"]:
        _canonicalize_symmetric_edges(c, app, d["name"])
    return d, changed, old_endpoints


def prune_forward_relations(c, app, from_type, keep_keys):
    """On a replace (merge=false), drop relations authored on ``from_type`` that
    are no longer listed — along with their edges. Inverse relations (authored on
    the other type) are never touched here. Returns the endpoint types of every
    dropped relation (both sides — dropping one rewires both projected bodies).
    """
    endpoints = set()
    rows = c.list_association_schemas_from_type(app, from_type)
    for r in rows:
        if r["forward_name"] not in keep_keys:
            c.delete_edges_for_assoc(app, r["name"])
            c.delete_association_schema(app, r["name"])
            endpoints.update((r["from_type"], r["to_type"]))
    return endpoints


def delete_relations_touching_type(c, app, type_name):
    """Delete every relationship (and its edges) in ``app`` where ``type_name``
    is an endpoint. Used when a whole object type is deleted. Neighbor objects on
    the other side are NOT removed by this — only the relationship metadata + edges.
    Returns the endpoint types of every deleted relationship.
    """
    endpoints = set()
    rows = c.list_association_schemas_touching_type(app, type_name)
    for r in rows:
        c.delete_edges_for_assoc(app, r["name"])
        endpoints.update((r["from_type"], r["to_type"]))
    c.delete_association_schemas_touching_type(app, type_name)
    return endpoints


def _canonicalize_symmetric_edges(c, app, name):
    """Rewrite a now-symmetric relationship's edges into canonical (sorted)
    order and drop reverse duplicates, so A–B and B–A collapse to one row.
    """
    rows = c.list_edges(app, name)
    canon = {}
    for r in rows:
        a, b = sorted((r["from_guid"], r["to_guid"]))
        prev = canon.get((a, b))
        if prev is None or r["created_at"] < prev:
            canon[(a, b)] = r["created_at"]
    c.delete_edges_for_assoc(app, name)
    for (a, b), created in canon.items():
        c.insert_edge_ignore(app, name, a, b, created)


def neighbors_touching_object(c, app, obj_guid, type_name):
    """Every neighbor sharing an edge with ``obj_guid``, as ``(type, guid)``
    pairs. Called before a delete removes those edges, so the change record can
    name the objects whose projected bodies the delete silently rewrites (§5.3
    of the streaming spec)."""
    pairs = set()
    for r in c.list_association_schemas_touching_type(app, type_name):
        for e in c.list_edges_touching_guids(app, r["name"], [obj_guid]):
            if e["from_guid"] == obj_guid:
                pairs.add((r["to_type"], e["to_guid"]))
            if e["to_guid"] == obj_guid:
                pairs.add((r["from_type"], e["from_guid"]))
    return sorted(pairs)


# --- relation "views" (one per relation a type exposes) -----------------------


def _all_assoc_schemas(app, c=None):
    return (c or db.store()).list_association_schemas(app)


def relation_views(app, type_name, c=None):
    """All relations visible on ``type_name`` in ``app``, each as a view dict:

        {key, assoc_name, side, mult, neighbor_type, cardinality, symmetric}

    ``side`` is 'from', 'to', or 'sym' — how this type sits on the edge.
    ``mult`` is 'one'/'many' — whether the value is a scalar guid or a list.
    A self-relation (non-symmetric) yields two views: forward and inverse.
    """
    views = []
    for s in _all_assoc_schemas(app, c):
        sym = bool(s["symmetric"])
        if sym:
            if type_name in (s["from_type"], s["to_type"]):
                views.append({
                    "key": s["forward_name"], "assoc_name": s["name"], "side": "sym",
                    "mult": _mult(s["cardinality"], "from"),
                    "neighbor_type": s["from_type"],   # == to_type for symmetric
                    "cardinality": s["cardinality"], "symmetric": True,
                })
            continue
        if s["from_type"] == type_name:
            views.append({
                "key": s["forward_name"], "assoc_name": s["name"], "side": "from",
                "mult": _mult(s["cardinality"], "from"), "neighbor_type": s["to_type"],
                "cardinality": s["cardinality"], "symmetric": False,
            })
        if s["to_type"] == type_name:
            views.append({
                "key": s["inverse_name"], "assoc_name": s["name"], "side": "to",
                "mult": _mult(s["cardinality"], "to"), "neighbor_type": s["from_type"],
                "cardinality": s["cardinality"], "symmetric": False,
            })
    return views


def relation_keys(app, type_name, c=None):
    return {v["key"] for v in relation_views(app, type_name, c)}


def schema_relations(app, type_name, c=None):
    """Return (relations, inverse_relations) dicts for a type's schema document.

    ``relations`` are authored on this type (editable); ``inverse_relations``
    are the read-only mirror of relations authored on another type.
    """
    relations, inverse = {}, {}
    for s in _all_assoc_schemas(app, c):
        sym = bool(s["symmetric"])
        if sym and type_name in (s["from_type"], s["to_type"]):
            relations[s["forward_name"]] = {
                "to": s["to_type"], "cardinality": s["cardinality"],
                "symmetric": True, "description": s["forward_description"],
            }
            continue
        if not sym and s["from_type"] == type_name:
            relations[s["forward_name"]] = {
                "to": s["to_type"], "cardinality": s["cardinality"],
                "inverse": s["inverse_name"], "symmetric": False,
                "description": s["forward_description"],
                "inverse_description": s["inverse_description"],
            }
        if not sym and s["to_type"] == type_name:
            inverse[s["inverse_name"]] = {
                "to": s["from_type"], "cardinality": _flip(s["cardinality"]),
                "inverse": s["forward_name"], "symmetric": False,
                "via_type": s["from_type"], "via_relation": s["forward_name"],
                "readonly": True, "description": s["inverse_description"],
            }
    return relations, inverse


def _flip(cardinality):
    frm, to = cardinality.split("_to_")
    return f"{to}_to_{frm}"


# --- projecting relations onto object bodies (read) ---------------------------


def project_relations(app, guids, type_name):
    """Map each guid to its relation values: {guid: {relation_key: guid|[guid]}}.

    Batched: one query per relationship type touching the page of objects, so a
    list read does not fan out into a query per object. Scoped to ``app``.
    """
    views = relation_views(app, type_name)
    result = {g: {} for g in guids}
    if not guids:
        return result
    # Seed every relation with its empty value so the shape is stable.
    for g in guids:
        for v in views:
            result[g][v["key"]] = [] if v["mult"] == "many" else None
    if not views:
        return result

    gset = set(guids)
    by_assoc = {}
    for v in views:
        by_assoc.setdefault(v["assoc_name"], []).append(v)

    for assoc_name, vs in by_assoc.items():
        rows = sorted(
            db.store().list_edges_touching_guids(app, assoc_name, guids),
            key=lambda r: (r.get("created_at"), str(r.get("id"))),
        )
        for v in vs:
            side, key, mult = v["side"], v["key"], v["mult"]
            for r in rows:
                fg, tg = r["from_guid"], r["to_guid"]
                if side == "from":
                    if fg in gset:
                        _assign(result[fg], key, mult, tg)
                elif side == "to":
                    if tg in gset:
                        _assign(result[tg], key, mult, fg)
                else:  # symmetric: object may be on either end
                    if fg in gset:
                        _assign(result[fg], key, mult, tg)
                    if tg in gset:
                        _assign(result[tg], key, mult, fg)
    return result


def _assign(bucket, key, mult, neighbor):
    if mult == "many":
        bucket[key].append(neighbor)
    elif bucket.get(key) is None:
        bucket[key] = neighbor


# --- writing relations as fields (set-as-field, last-write-wins) --------------


def apply_relation_writes(c, app, obj_guid, type_name, rel_part):
    """Apply the relation keys present in an object write.

    ``rel_part`` maps relation keys to their desired value (a guid / list of
    guids, or null/[] to clear). Only listed relations are touched; others are
    left as-is. Set semantics: the listed value becomes the relation's full set.

    Returns the neighbors whose edges actually changed, as ``(type, guid)``
    pairs — (old ∖ new) ∪ (new ∖ old); a neighbor merely re-listed is excluded.
    Streaming's §5.3 both-ends rule rides on this.
    """
    views = {v["key"]: v for v in relation_views(app, type_name, c)}
    touched = []
    for key, value in rel_part.items():
        view = views[key]
        desired = _desired_guids(key, view, value)
        touched.extend(_set_relation(c, app, obj_guid, view, desired, type_name))
    return touched


def _desired_guids(key, view, value):
    if view["mult"] == "one":
        if value in (None, ""):
            return []
        if not isinstance(value, str):
            raise bad_request(
                f"Relation '{key}' is to-one; expected a single guid string or null, "
                f"got {type(value).__name__}."
            )
        return [value]
    # to-many
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise bad_request(
            f"Relation '{key}' is to-many; expected a list of guids (or [] to clear), "
            f"got {type(value).__name__}."
        )
    seen, out = set(), []
    for g in value:
        if not isinstance(g, str):
            raise bad_request(f"Relation '{key}' values must be guid strings.")
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _set_relation(c, app, obj_guid, view, desired, type_name):
    name = view["assoc_name"]
    side = view["side"]

    # Current neighbors for this relation, from obj's point of view.
    rows = c.list_edges_for_object_side(app, name, side, obj_guid)
    current = {}
    for r in rows:
        if side == "from":
            nb = r["to_guid"]
        elif side == "to":
            nb = r["from_guid"]
        else:
            nb = r["to_guid"] if r["from_guid"] == obj_guid else r["from_guid"]
        current[nb] = r["id"]

    desired_set = set(desired)
    # Remove edges no longer wanted.
    for nb, eid in current.items():
        if nb not in desired_set:
            c.delete_edge_by_id(eid)

    other_mult = _mult(view["cardinality"], "to" if side == "from" else "from")
    ts = now_iso()
    evicted = []
    for nb in desired:
        if nb in current:
            continue
        _validate_target(c, app, view, obj_guid, nb)
        # Last-write-wins: if the neighbor's slot on the other side is single and
        # already taken, steal it. Enumerate the evicted holder before deleting
        # so its projected body change reaches its stream (§5.3): the holder sits
        # on obj's side of the edge, so it is of obj's own type.
        if other_mult == "one":
            evicted.extend(_free_target_slot(c, app, name, side, nb, obj_guid))
        fg, tg = _edge_endpoints(side, obj_guid, nb)
        c.insert_edge_ignore(app, name, fg, tg, ts)

    ntype = view["neighbor_type"]
    return ([(ntype, nb) for nb in current if nb not in desired_set]
            + [(ntype, nb) for nb in desired if nb not in current]
            + [(type_name, e) for e in evicted])


def _edge_endpoints(side, obj_guid, neighbor):
    if side == "from":
        return obj_guid, neighbor
    if side == "to":
        return neighbor, obj_guid
    return tuple(sorted((obj_guid, neighbor)))   # symmetric: canonical order


def _free_target_slot(c, app, name, side, neighbor, writer):
    """Delete edges occupying ``neighbor``'s single slot on the other side, and
    return the guids of the holders being evicted (excluding ``writer``, which is
    (re)linking, not losing a link). Enumerate before the blind delete."""
    holders = []
    for r in c.list_edges_touching_guids(app, name, [neighbor]):
        if side == "from" and r["to_guid"] == neighbor:
            h = r["from_guid"]
        elif side == "to" and r["from_guid"] == neighbor:
            h = r["to_guid"]
        elif side == "sym" and neighbor in (r["from_guid"], r["to_guid"]):
            h = r["to_guid"] if r["from_guid"] == neighbor else r["from_guid"]
        else:
            continue
        if h != writer:
            holders.append(h)
    c.delete_edges_for_target_slot(app, name, side, neighbor)
    return holders


def _validate_target(c, app, view, obj_guid, neighbor):
    if neighbor == obj_guid:
        raise bad_request("Self-referential edges (an object related to itself) are not allowed.")
    # The target must be an object in the SAME app — relations never cross apps.
    row = c.get_object(app, neighbor)
    if row is None:
        raise bad_request(
            f"Relation '{view['key']}' target '{neighbor}' does not exist.")
    if row["object_type"] != view["neighbor_type"]:
        raise bad_request(
            f"Relation '{view['key']}' expects a '{view['neighbor_type']}', but "
            f"'{neighbor}' is a '{row['object_type']}'.")
