"""HTTP route definitions. Pure glue between the router and the logic modules.

Two surfaces only:

* **Schema** (the coding agent reshapes the data model):
  GET/PUT/DELETE ``/schema`` and ``/schema/{type}``.
* **Objects** (the frontend reads/writes data, including relations as fields):
  ``/objects/{type}`` and ``/object/{guid}``.

Relations are not their own endpoints — they are declared inside a type's schema
and read/written as fields on objects.
"""

from . import objects as objs
from . import schema as sch
from .errors import bad_request
from .router import Router

router = Router()


def _obj_body(req):
    """Return the request body as a dict, or raise 400.

    An empty/absent body is treated as ``{}`` (a valid empty write); a non-object
    JSON value (list, string, number, ``null``) is rejected rather than silently
    dropped.
    """
    b = req.body
    if b is None or b == {}:
        return {}
    if not isinstance(b, dict):
        raise bad_request("Request body must be a JSON object.")
    return b


# --- meta ---------------------------------------------------------------------


@router.route("GET", "/")
def root(req):
    return {
        "name": "MorphDB",
        "version": __import__("morphdb").__version__,
        "description": "Schema-fluid, API-stable database for AI-generated apps.",
        "docs": "GET /help for the full endpoint reference.",
    }


@router.route("GET", "/health")
def health(req):
    return {"status": "ok"}


@router.route("GET", "/help")
def help_(req):
    return {"endpoints": ENDPOINT_REFERENCE}


# --- schema (for the coding agent) --------------------------------------------


@router.route("GET", "/schema")
def full_schema(req):
    return {"types": sch.list_type_docs()}


@router.route("GET", "/schema/{type}")
def get_type(req):
    return sch.get_type_doc(req.params["type"], required=True)


def _type_body(body):
    """Parse a schema-write body into (fields, relations, merge).

    Accepts a structured doc ``{fields?, relations?, merge?}`` or, as a
    shorthand, a bare ``{name: type}`` field map. A ``None`` fields/relations
    means "leave that part untouched".
    """
    if not isinstance(body, dict):
        raise bad_request(
            "Body must be a schema document {fields?, relations?, merge?} or a "
            "bare field map."
        )
    if any(k in body for k in ("fields", "relations", "merge")):
        fields = body.get("fields")
        relations = body.get("relations")
        if fields is not None and not isinstance(fields, dict):
            raise bad_request("'fields' must be an object mapping name -> type.")
        return fields, relations, bool(body.get("merge", False))
    return body, None, False     # bare field map


@router.route("PUT", "/schema/{type}")
def put_type(req):
    fields, relations, merge = _type_body(_obj_body(req))
    return sch.upsert_type(req.params["type"], fields=fields,
                           relations=relations, merge=merge)


@router.route("DELETE", "/schema/{type}")
def delete_type(req):
    return sch.delete_type(req.params["type"])


# --- objects (for the website) ------------------------------------------------


@router.route("POST", "/objects/{type}")
def create_object(req):
    return 201, objs.create_object(req.params["type"], _obj_body(req))


@router.route("GET", "/objects/{type}")
def list_objects(req):
    q = dict(req.query)
    limit = q.pop("limit", objs.DEFAULT_LIMIT)
    offset = q.pop("offset", 0)
    sort = q.pop("sort", None)
    order = q.pop("order", "asc")
    # everything left in q is a field filter
    return objs.list_objects(
        req.params["type"], filters=q, limit=limit, offset=offset,
        sort=sort, order=order,
    )


@router.route("GET", "/objects/{type}/{guid}")
def get_object_typed(req):
    return objs.get_object(req.params["guid"], object_type=req.params["type"])


@router.route("PUT", "/objects/{type}/{guid}")
def put_object(req):
    return objs.upsert_object(req.params["type"], req.params["guid"],
                              _obj_body(req), partial=False)


@router.route("PATCH", "/objects/{type}/{guid}")
def patch_object(req):
    return objs.upsert_object(req.params["type"], req.params["guid"],
                              _obj_body(req), partial=True)


@router.route("DELETE", "/objects/{type}/{guid}")
def delete_object(req):
    return objs.delete_object(req.params["guid"])


@router.route("GET", "/object/{guid}")
def get_object_by_guid(req):
    return objs.get_object(req.params["guid"])


# --- self-documenting reference (served at GET /help) -------------------------

ENDPOINT_REFERENCE = {
    "schema endpoints (you, the agent — reshape the data model)": {
        "GET /schema": "View all type schemas (fields + relations + inverse relations).",
        "GET /schema/{type}": "View one type's schema.",
        "PUT /schema/{type}": (
            "Create/replace a type. Body: {fields?, relations?, merge?} or a bare "
            "field map. merge:true adds without dropping; merge:false replaces. "
            "Absent 'fields'/'relations' are left untouched."
        ),
        "DELETE /schema/{type}": (
            "Delete a type, its objects, and edges touching them. Neighbor objects "
            "of other types are NOT deleted."
        ),
    },
    "object endpoints (your frontend — read/write data)": {
        "POST /objects/{type}": "Create an object. Body: field + relation values. Returns it with _guid.",
        "GET /objects/{type}": "List/query. Query: field filters (field, field__gt, field__contains, field__in, ...), limit, offset, sort, order.",
        "GET /objects/{type}/{guid}": "Read one object (fields + relation guids).",
        "GET /object/{guid}": "Read one object by guid alone.",
        "PUT /objects/{type}/{guid}": "Replace an object's fields (create if absent). Relations present in the body are set.",
        "PATCH /objects/{type}/{guid}": "Merge fields into an object (create if absent). Relations present in the body are set.",
        "DELETE /objects/{type}/{guid}": "Delete an object and its edges (neighbors survive).",
    },
    "relations": {
        "declare": (
            "In a type's schema under 'relations': "
            "{\"assignee\": {\"to\": \"user\", \"cardinality\": \"many_to_one\", "
            "\"inverse\": \"tasks\"}}. Declared once; the inverse ('tasks') appears "
            "automatically on the other type."
        ),
        "read": "Relation values appear in the object body: a guid (to-one) or list of guids (to-many).",
        "write": "Set a relation like a field: {\"assignee\": \"<guid>\"} or {\"tags\": [\"<g1>\", \"<g2>\"]}. null/[] clears. Last write wins on conflict.",
        "symmetric": "Set symmetric:true (to == this type, one_to_one|many_to_many) for mutual links like friends — one shared label, edge counted once.",
    },
    "field_types": ["string", "number", "boolean", "json", "datetime"],
    "cardinalities": ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    "filter_operators": ["eq (default)", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"],
    "list_response_shape": {"objects": "[...]", "total": "int (full filtered count)",
                            "limit": "int", "offset": "int"},
    "notes": [
        "datetime values are validated as ISO-8601 (or epoch seconds) and normalized.",
        "number fields reject NaN/Infinity.",
        "schema edits are O(1) and lazy: after a field retype, an old-typed value reads as unset until rewritten.",
        "relations are stored as single-row edges and read/written as object fields; filtering is on fields, not relations.",
    ],
}
