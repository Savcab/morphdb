"""HTTP route definitions. Pure glue between the router and the logic modules.

Three surfaces:

* **Apps** (one MorphDB instance, many independent websites):
  POST ``/app`` to register, DELETE ``/app/{key}`` to delete (cascades). There
  is deliberately no "list apps" endpoint — you only address an app you already
  hold the key for.
* **Schema** (the coding agent reshapes the data model):
  GET/PUT/DELETE ``/schema`` and ``/schema/{type}``.
* **Objects** (the frontend reads/writes data, including relations as fields):
  ``/objects/{type}`` and ``/object/{guid}``.

Every schema and object request must carry its app via the ``X-App-Key`` header;
``apps.require_app`` resolves and validates it. Relations are not their own
endpoints — they are declared inside a type's schema and read/written as fields
on objects.
"""

from . import apps
from . import objects as objs
from . import schema as sch
from .errors import ApiError, bad_request
from .router import Request, Router

router = Router()


def dispatch(method, path, query, body, headers):
    """Match a request to its handler and run it; return ``(status, payload)``.

    The transport-neutral core every front end shares (the stdlib HTTP server and
    the Lambda adapter): route lookup, 404/405, building the :class:`Request`, and
    normalizing a handler's return into a ``(status, payload)`` pair. Body reading
    and response encoding stay with each transport. An ``ApiError`` — raised here
    for an unmatched route, or by a handler — propagates to the caller to render.
    """
    handler, params, path_matched = router.match(method, path)
    if handler is None:
        if path_matched:
            raise ApiError(405, "method_not_allowed", f"{method} not allowed on {path}.")
        raise ApiError(404, "not_found", f"No route for {method} {path}. See GET /help.")
    req = Request(method, path, params, query, body, headers)
    result = handler(req)
    return result if isinstance(result, tuple) else (200, result)


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
    from . import streams
    return {
        "name": "MorphDB",
        "version": __import__("morphdb").__version__,
        "description": "Coding-agent-friendly, multi-tenant backend for vibe-coded websites.",
        "docs": "GET /help for the full endpoint reference.",
        # Transport capability: true only on a transport that can hold SSE
        # connections open (the stdlib server). Request/response deploys (Lambda)
        # report false; the SDK reads this to decide stream vs poll.
        "streaming": streams.STREAMING,
    }


@router.route("GET", "/health")
def health(req):
    return {"status": "ok"}


@router.route("GET", "/help")
def help_(req):
    return {"endpoints": ENDPOINT_REFERENCE}


# --- apps (register a website; delete one and everything under it) ------------


@router.route("POST", "/app")
def register_app(req):
    body = _obj_body(req)
    key = body.get("key")
    if not key or not isinstance(key, str):
        raise bad_request(
            "Provide an app key: {\"key\": \"my-app\"}. Pick a unique, memorable "
            "string and reuse it as the X-App-Key header on every request."
        )
    return 201, apps.register_app(key)


@router.route("DELETE", "/app/{key}")
def delete_app(req):
    return apps.delete_app(req.params["key"])


# --- schema (for the coding agent) --------------------------------------------


@router.route("GET", "/schema")
def full_schema(req):
    app = apps.require_app(req)
    return {"types": sch.list_type_docs(app)}


@router.route("GET", "/schema/{type}")
def get_type(req):
    app = apps.require_app(req)
    return sch.get_type_doc(app, req.params["type"], required=True)


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
    app = apps.require_app(req)
    fields, relations, merge = _type_body(_obj_body(req))
    return sch.upsert_type(app, req.params["type"], fields=fields,
                           relations=relations, merge=merge)


@router.route("DELETE", "/schema/{type}")
def delete_type(req):
    app = apps.require_app(req)
    return sch.delete_type(app, req.params["type"])


# --- objects (for the website) ------------------------------------------------


@router.route("POST", "/objects/{type}")
def create_object(req):
    app = apps.require_app(req)
    return 201, objs.create_object(app, req.params["type"], _obj_body(req))


@router.route("GET", "/objects/{type}")
def list_objects(req):
    app = apps.require_app(req)
    q = dict(req.query)
    limit = q.pop("limit", objs.DEFAULT_LIMIT)
    offset = q.pop("offset", 0)
    sort = q.pop("sort", None)
    order = q.pop("order", "asc")
    include = q.pop("include", None)
    # everything left in q is a field filter
    return objs.list_objects(
        app, req.params["type"], filters=q, limit=limit, offset=offset,
        sort=sort, order=order, include=include,
    )


@router.route("GET", "/objects/{type}/{guid}")
def get_object_typed(req):
    app = apps.require_app(req)
    return objs.get_object(app, req.params["guid"], object_type=req.params["type"],
                           include=req.query.get("include"))


@router.route("PUT", "/objects/{type}/{guid}")
def put_object(req):
    app = apps.require_app(req)
    return objs.upsert_object(app, req.params["type"], req.params["guid"],
                              _obj_body(req), partial=False)


@router.route("PATCH", "/objects/{type}/{guid}")
def patch_object(req):
    app = apps.require_app(req)
    return objs.upsert_object(app, req.params["type"], req.params["guid"],
                              _obj_body(req), partial=True)


@router.route("DELETE", "/objects/{type}/{guid}")
def delete_object(req):
    app = apps.require_app(req)
    return objs.delete_object(app, req.params["guid"])


@router.route("GET", "/object/{guid}")
def get_object_by_guid(req):
    app = apps.require_app(req)
    return objs.get_object(app, req.params["guid"], include=req.query.get("include"))


# --- streaming (served by the stdlib transport; 501 everywhere else) ----------


@router.route("GET", "/stream/{type}")
def stream(req):
    # The stdlib server intercepts /stream/ before dispatch and serves SSE. This
    # handler is only reached through the request/response path (Lambda,
    # embedders), which cannot hold a connection open — so it teaches the fix.
    raise ApiError(
        501, "not_implemented",
        "This deployment is request/response and cannot stream. Use the stdlib "
        "server (morphdb start) for live queries; watch() falls back to polling "
        "here automatically.")


# --- self-documenting reference (served at GET /help) -------------------------

ENDPOINT_REFERENCE = {
    "_apps": (
        "One MorphDB instance hosts many apps (one per website). Register an app, "
        "then send its key as the 'X-App-Key' header on EVERY schema and object "
        "request. Apps are isolated: type names may repeat across apps. There is "
        "no list-apps endpoint by design."
    ),
    "app endpoints (register / delete a website)": {
        "POST /app": "Register an app. Body: {\"key\": \"my-app\"}. 409 if the key is taken. Remember the key — there is no way to list it back.",
        "DELETE /app/{key}": "Delete an app and cascade-delete all its schemas, objects, relations, and edges.",
    },
    "schema endpoints (you, the agent — reshape the data model)": {
        "GET /schema": "View all type schemas (fields + relations + inverse relations) for this app.",
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
        "GET /objects/{type}": "List/query. Query: field filters on INDEXED fields (field, field__gt, field__contains, field__in, ...), relation filters (rel=<guid>, rel__in, rel__ne, rel__exists), limit, offset, sort (indexed field), order, include.",
        "GET /stream/{type}": (
            "Live query over Server-Sent Events (text/event-stream). Same query "
            "grammar as the list endpoint, plus mode=snapshot|delta, refresh=<ms> "
            "(snapshot), and app_key=<key> (EventSource can't set headers). "
            "Emits init, then snapshot (whole result) — enter/update/leave deltas "
            "arrive in delta mode. Only on the stdlib server; 501 on "
            "request/response deploys (see GET / \"streaming\")."
        ),
        "GET /objects/{type}/{guid}": "Read one object (fields + relation guids). ?include=<paths> hydrates relations into nested objects.",
        "GET /object/{guid}": "Read one object by guid alone. Supports ?include=<paths>.",
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
        "filter": "Filter a list by a relation, like an ORM foreign key: ?assignee=<guid> (linked to it), ?assignee__in=<g1>,<g2>, ?assignee__ne=<guid>, ?assignee__exists=true|false. Combine with field filters/sort/pagination. Resolved through the indexed edge table, so it is index-backed.",
        "include": "Hydrate relations into nested objects instead of guids: ?include=author,comments,comments.author (comma-separated, dots nest). Works on the list and single-object reads; read-only, depth <= 4, batched (no N+1). Without include a relation stays a guid / list of guids.",
        "symmetric": "Set symmetric:true (to == this type, one_to_one|many_to_many) for mutual links like friends — one shared label, edge counted once.",
    },
    "headers": {
        "X-App-Key": "Required on every schema and object request: the key of the app you registered. Missing -> 400, unknown -> 404.",
    },
    "field_types": ["string", "number", "boolean", "json", "datetime"],
    "cardinalities": ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    "filter_operators": ["eq (default)", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"],
    "list_response_shape": {"objects": "[...]", "total": "int (full filtered count)",
                            "limit": "int", "offset": "int"},
    "backend_notes": {
        "sqlite": "Default zero-dependency backend. Good for local/prototype use.",
        "postgres": "Managed/networked SQL backend via morphdb[postgres]; same API.",
        "dynamodb": (
            "Optional backend via morphdb[dynamodb]. Same API and exact totals, "
            "but large offsets, broad filtered reads, contains filters, negative "
            "relation filters, and broad sorts can require reading/filtering more "
            "items internally. Prefer selective indexed filters and small offsets."
        ),
    },
    "notes": [
        "datetime values are validated as ISO-8601 (or epoch seconds) and normalized.",
        "number fields reject NaN/Infinity.",
        "schema edits are O(1) and lazy: after a field retype, an old-typed value reads as unset until rewritten.",
        "a field is filterable/sortable only when declared with index:true; otherwise a filter/sort on it returns 400. Enabling the flag backfills existing objects; json fields can't be indexed.",
        "relations are stored as single-row edges and read/written as object fields; they are filterable on the list endpoint (?rel=<guid>, __in/__ne/__exists), resolved through the indexed edge table — think ORM foreign key, not a separate join.",
        "include hydrates relations into nested objects on reads (?include=author,comments.author); read-only, depth <= 4, batched (no N+1). Writes stay flat — create/update with guids, never nested objects.",
        "relation targets must be objects in the same app; cross-app links are rejected.",
    ],
}
