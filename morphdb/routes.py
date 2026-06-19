"""HTTP route definitions. Pure glue between the router and the logic modules."""

from . import associations as assoc
from . import objects as objs
from . import schema as sch
from .errors import bad_request
from .router import Router

router = Router()


def _obj_body(req):
    """Return the request body as a dict, or raise 400.

    An empty/absent body is treated as ``{}`` (a valid empty write), but a
    non-object JSON value (list, string, number, ``null``) is rejected rather
    than silently dropped — that would create blank objects from bad input.
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


@router.route("GET", "/schema")
def full_schema(req):
    return {
        "objects": sch.list_object_schemas(),
        "associations": assoc.list_association_schemas(),
    }


# --- object schemas (for the coding agent) ------------------------------------


@router.route("GET", "/schemas/objects")
def list_object_schemas(req):
    return {"schemas": sch.list_object_schemas()}


@router.route("GET", "/schemas/objects/{type}")
def get_object_schema(req):
    return sch.get_object_schema(req.params["type"], required=True)


def _fields_and_merge(body):
    if isinstance(body, dict) and "fields" in body and isinstance(body["fields"], dict):
        return body["fields"], bool(body.get("merge", False))
    if isinstance(body, dict):
        return body, False
    raise bad_request("Body must be an object of field definitions, or {fields, merge}.")


@router.route("POST", "/schemas/objects")
def create_object_schema(req):
    body = req.body if isinstance(req.body, dict) else {}
    name = body.get("name")
    if not name:
        raise bad_request("POST /schemas/objects requires a 'name'.")
    fields = body.get("fields", {})
    merge = bool(body.get("merge", False))
    return 201, sch.upsert_object_schema(name, fields, merge=merge)


@router.route("PUT", "/schemas/objects/{type}")
def put_object_schema(req):
    fields, merge = _fields_and_merge(req.body)
    return sch.upsert_object_schema(req.params["type"], fields, merge=merge)


@router.route("DELETE", "/schemas/objects/{type}")
def delete_object_schema(req):
    cascade = req.query_bool("cascade", default=True)
    return sch.delete_object_schema(req.params["type"], cascade=cascade)


@router.route("POST", "/schemas/objects/{type}/delete-fields")
def delete_object_fields_post(req):
    body = req.body if isinstance(req.body, dict) else {}
    return sch.delete_object_fields(req.params["type"], body.get("fields"))


@router.route("DELETE", "/schemas/objects/{type}/fields")
def delete_object_fields_del(req):
    body = req.body if isinstance(req.body, dict) else {}
    return sch.delete_object_fields(req.params["type"], body.get("fields"))


# --- association schemas (for the coding agent) -------------------------------


@router.route("GET", "/schemas/associations")
def list_assoc_schemas(req):
    return {"schemas": assoc.list_association_schemas()}


@router.route("GET", "/schemas/associations/{name}")
def get_assoc_schema(req):
    return assoc.get_association_schema(req.params["name"], required=True)


def _assoc_schema_args(body):
    return dict(
        from_type=body.get("from_type"),
        to_type=body.get("to_type"),
        forward_name=body.get("forward_name"),
        # inverse_name is optional for symmetric relationships (defaults to
        # forward_name); validated in the logic layer otherwise.
        inverse_name=body.get("inverse_name"),
        cardinality=body.get("cardinality"),
        # Pass through raw; the logic layer parses it (bool("false") is True!).
        symmetric=body.get("symmetric", False),
    )


@router.route("POST", "/schemas/associations")
def create_assoc_schema(req):
    body = _obj_body(req)
    name = body.get("name")
    if not name:
        raise bad_request("POST /schemas/associations requires a 'name'.")
    args = _assoc_schema_args(body)
    _require(args, "from_type", "to_type", "forward_name", "cardinality")
    return 201, assoc.upsert_association_schema(name, **args)


@router.route("PUT", "/schemas/associations/{name}")
def put_assoc_schema(req):
    body = _obj_body(req)
    args = _assoc_schema_args(body)
    _require(args, "from_type", "to_type", "forward_name", "cardinality")
    return assoc.upsert_association_schema(req.params["name"], **args)


@router.route("DELETE", "/schemas/associations/{name}")
def delete_assoc_schema(req):
    cascade = req.query_bool("cascade", default=True)
    return assoc.delete_association_schema(req.params["name"], cascade=cascade)


def _require(args, *keys):
    missing = [k for k in keys if args.get(k) in (None, "")]
    if missing:
        raise bad_request(f"Missing required field(s): {missing}.")


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
    q.pop("expand", None)
    # everything left in q is a filter
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


@router.route("GET", "/objects/{type}/{guid}/associations")
def object_associations_typed(req):
    # Type-check the {type} segment for consistency with GET /objects/{type}/{guid}.
    objs.get_object(req.params["guid"], object_type=req.params["type"])
    return _read_associations(req, req.params["guid"])


# --- objects by guid alone (guids are globally unique) ------------------------


@router.route("GET", "/object/{guid}")
def get_object_by_guid(req):
    return objs.get_object(req.params["guid"])


@router.route("GET", "/object/{guid}/associations")
def object_associations(req):
    return _read_associations(req, req.params["guid"])


def _read_associations(req, guid):
    return assoc.get_associations(
        guid,
        name=req.query.get("name"),
        relation=req.query.get("relation"),
        direction=req.query.get("direction"),
        expand=req.query_bool("expand", default=False),
    )


# --- associations (for the website) -------------------------------------------


@router.route("POST", "/associations")
def create_association(req):
    body = _obj_body(req)
    name = body.get("assoc_name") or body.get("name")
    if not name:
        raise bad_request("Provide the association type as 'assoc_name' (or 'name').")
    from_guid = body.get("from_guid")
    to_guid = body.get("to_guid")
    if not from_guid or not to_guid:
        raise bad_request("Provide 'from_guid' and 'to_guid'.")
    replace = req.query_bool("replace", default=False) or bool(body.get("replace", False))
    return 201, assoc.create_association(name, from_guid, to_guid, replace=replace)


@router.route("DELETE", "/associations")
def delete_association(req):
    return _do_delete_association(_obj_body(req))


@router.route("POST", "/associations/delete")
def delete_association_post(req):
    return _do_delete_association(_obj_body(req))


def _do_delete_association(body):
    name = body.get("assoc_name") or body.get("name")
    from_guid = body.get("from_guid")
    to_guid = body.get("to_guid")
    if not (name and from_guid and to_guid):
        raise bad_request("Provide 'assoc_name', 'from_guid', and 'to_guid'.")
    return assoc.delete_association(name, from_guid, to_guid)


# --- self-documenting reference (served at GET /help) -------------------------

ENDPOINT_REFERENCE = {
    "schema_management (for the agent)": {
        "GET /schema": "View all object + association schemas at once.",
        "GET /schemas/objects": "List object type schemas.",
        "GET /schemas/objects/{type}": "View one object type schema.",
        "POST /schemas/objects": "Create/replace a type. Body: {name, fields, merge?}.",
        "PUT /schemas/objects/{type}": "Create/replace a type. Body: {fields, merge?} or a raw fields map.",
        "DELETE /schemas/objects/{type}?cascade=true": "Delete a type (and, by default, its objects).",
        "POST /schemas/objects/{type}/delete-fields": "Remove fields. Body: {fields: [..]}.",
        "GET /schemas/associations": "List association (relationship) types.",
        "POST /schemas/associations": "Create/replace a relationship type. Body: {name, from_type, to_type, forward_name, inverse_name, cardinality, symmetric?}. Set symmetric:true (from_type==to_type, one_to_one|many_to_many) for mutual relations like friends.",
        "DELETE /schemas/associations/{name}?cascade=true": "Delete a relationship type (and its edges).",
    },
    "data (for the website)": {
        "POST /objects/{type}": "Create an object. Body: field values. Returns the object with its _guid.",
        "GET /objects/{type}": "List/query objects. Query: field filters (field, field__gt, field__contains, field__in, ...), limit, offset, sort, order.",
        "GET /objects/{type}/{guid}": "Read one object.",
        "GET /object/{guid}": "Read one object by guid alone.",
        "PUT /objects/{type}/{guid}": "Replace an object's data (create if absent).",
        "PATCH /objects/{type}/{guid}": "Merge fields into an object (create if absent).",
        "DELETE /objects/{type}/{guid}": "Delete an object and its edges.",
        "GET /object/{guid}/associations": "List edges touching an object. Query: name, relation, direction, expand.",
        "POST /associations": "Create an edge. Body: {assoc_name, from_guid, to_guid}. Query/body: replace.",
        "DELETE /associations": "Delete an edge. Body: {assoc_name, from_guid, to_guid}.",
    },
    "field_types": ["string", "number", "boolean", "json", "datetime"],
    "cardinalities": ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    "filter_operators": ["eq (default)", "ne", "gt", "gte", "lt", "lte", "contains", "in", "exists"],
    "list_response_shape": {"objects": "[...]", "total": "int (full filtered count)",
                            "limit": "int", "offset": "int"},
    "notes": [
        "datetime values are validated as ISO-8601 (or epoch seconds) and normalized.",
        "number fields reject NaN/Infinity.",
        "defaults are materialized on write, so they are queryable like any value.",
    ],
}
