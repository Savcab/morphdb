"""A hand-rolled, zero-dependency MCP server exposing MorphDB's schema + app
operations as tools for a coding agent (e.g. Claude Code).

Transport is stdio with newline-delimited JSON-RPC 2.0 — the MCP stdio framing.
The agent's host spawns ``morphdb mcp`` and speaks JSON-RPC over stdin/stdout;
this server translates each ``tools/call`` into an HTTP request against the
MorphDB backend daemon (the very same one ``morphdb start`` runs) and returns the
result. It owns no data: it is a thin HTTP *client* of the backend, so there is
always exactly one SQLite writer.

If the backend is not running it is auto-started, unless a hosted ``MORPHDB_HOST``
is configured — in that case calls go there and nothing local is managed.

This is deliberately implemented in pure stdlib (no ``mcp`` SDK) so the whole
package stays dependency-free; the MCP wire protocol used here is the small,
stable core (initialize / tools.list / tools.call / ping).
"""

import json
import os
import sys
import urllib.error
import urllib.request

from .. import __version__

# Advertised when the client doesn't pin a version. We echo the client's
# requested protocolVersion when present (our wire format is stable across the
# revisions that matter), which maximizes compatibility.
_DEFAULT_PROTOCOL = "2025-06-18"

FIELD_TYPES = ["string", "number", "boolean", "json", "datetime"]
CARDINALITIES = ["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


# --- backend plumbing ---------------------------------------------------------


class _ApiError(Exception):
    """A backend HTTP error or connectivity failure; message is safe to surface."""


def _target():
    """Base URL of the MorphDB backend to talk to.

    ``$MORPHDB_HOST`` (a hosted server) wins and is used as-is; nothing local is
    managed. Otherwise talk to the local daemon, auto-starting it if it is down,
    so the agent never sees "backend not running".
    """
    host = os.environ.get("MORPHDB_HOST", "").strip()
    if host:
        return host if "://" in host else "http://" + host
    from . import service
    st = service.status()
    if not st.get("running"):
        st, _ = service.start()
    return "http://{}:{}".format(st.get("host", service.DEFAULT_HOST),
                                 st.get("port", service.DEFAULT_PORT))


def _http(method, path, body=None, app=None):
    """One HTTP call to the backend. Returns parsed JSON (or None); raises
    :class:`_ApiError` with a readable message on any failure."""
    base = _target()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if app:
        req.add_header("X-App-Key", app)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            msg = raw.decode(errors="replace") or str(e.reason)
        raise _ApiError("backend error {}: {}".format(e.code, msg))
    except urllib.error.URLError as e:
        raise _ApiError(
            "cannot reach the MorphDB backend at {} ({}). Is it running? "
            "Try `morphdb start`, or set MORPHDB_HOST to a hosted server."
            .format(base, e.reason))


# --- small helpers shared by tool handlers ------------------------------------


def _req_str(args, key):
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _ApiError('missing required "{}" (a non-empty string).'.format(key))
    return v.strip()


def _need_app(args):
    """The app key for a scoped call: the ``app`` argument, else ``$MORPHDB_APP``."""
    app = (args.get("app") or os.environ.get("MORPHDB_APP") or "").strip()
    if not app:
        raise _ApiError(
            'no app key: pass "app" (the key you registered) or set $MORPHDB_APP. '
            "Register one first with the register_app tool.")
    return app


def _authoring_def(view):
    """Strip a relation's read-back doc down to the fields a PUT accepts (so the
    surviving relations can be re-authored when dropping one)."""
    out = {"to": view["to"], "cardinality": view["cardinality"]}
    if view.get("symmetric"):
        out["symmetric"] = True
    else:
        out["inverse"] = view.get("inverse")
    if view.get("description"):
        out["description"] = view["description"]
    if view.get("inverse_description"):
        out["inverse_description"] = view["inverse_description"]
    return out


# --- tool handlers (each returns a JSON-able result, or raises _ApiError) ------


def _t_register_app(args):
    key = (args.get("key") or "").strip()
    if not key:
        raise _ApiError('provide "key": a unique app key you choose and remember.')
    return _http("POST", "/app", {"key": key})


def _t_delete_app(args):
    key = (args.get("key") or "").strip()
    if not key:
        raise _ApiError('provide "key": the app to delete.')
    return _http("DELETE", "/app/" + key)


def _t_list_types(args):
    return _http("GET", "/schema", app=_need_app(args))


def _t_describe_type(args):
    app = _need_app(args)
    return _http("GET", "/schema/" + _req_str(args, "type"), app=app)


def _t_add_field(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    name = _req_str(args, "name")
    ftype = _req_str(args, "field_type")
    fdef = {"type": ftype}
    if args.get("default") is not None:
        fdef["default"] = args["default"]
    if args.get("required"):
        fdef["required"] = True
    # merge:true so existing fields/relations are untouched (idempotent re-runs).
    return _http("PUT", "/schema/" + type_,
                 {"merge": True, "fields": {name: fdef}}, app=app)


def _t_drop_field(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    name = _req_str(args, "name")
    current = _http("GET", "/schema/" + type_, app=app) or {}
    fields = current.get("fields", {})
    if name not in fields:
        raise _ApiError("type '{}' has no field '{}'.".format(type_, name))
    fields.pop(name)
    # Replace fields (merge:false) with the remainder; omit 'relations' to leave
    # them untouched.
    return _http("PUT", "/schema/" + type_,
                 {"merge": False, "fields": fields}, app=app)


def _t_add_relation(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    name = _req_str(args, "name")
    rel = {"to": _req_str(args, "to"), "cardinality": _req_str(args, "cardinality")}
    if args.get("symmetric"):
        rel["symmetric"] = True
    elif args.get("inverse"):
        rel["inverse"] = args["inverse"]
    else:
        raise _ApiError(
            'a non-symmetric relation needs "inverse" (the name the other side '
            'sees), or set "symmetric": true.')
    if args.get("description"):
        rel["description"] = args["description"]
    if args.get("inverse_description"):
        rel["inverse_description"] = args["inverse_description"]
    return _http("PUT", "/schema/" + type_,
                 {"merge": True, "relations": {name: rel}}, app=app)


def _t_drop_relation(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    name = _req_str(args, "name")
    current = _http("GET", "/schema/" + type_, app=app) or {}
    relations = current.get("relations", {})
    if name not in relations:
        inverse = current.get("inverse_relations", {})
        if name in inverse:
            via = inverse[name]
            raise _ApiError(
                "'{}' is an inverse relation, authored on type '{}' as '{}'. "
                "Drop it from that side.".format(
                    name, via.get("via_type"), via.get("via_relation")))
        raise _ApiError("type '{}' has no relation '{}'.".format(type_, name))
    relations.pop(name)
    remaining = {k: _authoring_def(v) for k, v in relations.items()}
    return _http("PUT", "/schema/" + type_,
                 {"merge": False, "relations": remaining}, app=app)


def _t_delete_type(args):
    app = _need_app(args)
    return _http("DELETE", "/schema/" + _req_str(args, "type"), app=app)


def _t_set_schema(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    doc = args.get("doc")
    if not isinstance(doc, dict):
        raise _ApiError(
            '"doc" must be a schema document object {fields?, relations?, merge?}.')
    return _http("PUT", "/schema/" + type_, doc, app=app)


def _t_query_objects(args):
    app = _need_app(args)
    type_ = _req_str(args, "type")
    path = "/objects/" + type_
    query = args.get("query")
    if isinstance(query, str) and query.strip():
        path += "?" + query.lstrip("?")
    return _http("GET", path, app=app)


# --- tool catalog -------------------------------------------------------------

_APP = {"app": {"type": "string", "description":
                "The app key you registered (sent as X-App-Key). "
                "Defaults to $MORPHDB_APP if set."}}


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required}}


TOOLS = [
    _tool("register_app",
          "Register a new app (tenant) — one per website. Pick a unique, memorable "
          "key; you reuse it on every later call and there is NO way to list keys "
          "back, so remember it. 409 if the key is taken.",
          {"key": {"type": "string", "description": "Unique app key you choose."}},
          ["key"]),
    _tool("delete_app",
          "Delete an app and CASCADE-delete everything under it (all its types, "
          "objects, relations, edges). Other apps are untouched. Irreversible.",
          {"key": {"type": "string", "description": "The app key to delete."}},
          ["key"]),
    _tool("list_types",
          "List every type in the app with its fields, relations, and inverse "
          "relations. Use this to see the current data model before editing it.",
          dict(_APP), []),
    _tool("describe_type",
          "Show one type's full schema (fields + relations + inverse relations).",
          {**_APP, "type": {"type": "string", "description": "Type name."}},
          ["type"]),
    _tool("add_field",
          "Add (or update) a field on a type. Idempotent merge — safe to re-run; "
          "creates the type if it does not exist. O(1), no data migration.",
          {**_APP,
           "type": {"type": "string", "description": "Type name."},
           "name": {"type": "string", "description": "Field name."},
           "field_type": {"type": "string", "enum": FIELD_TYPES,
                          "description": "One of: " + ", ".join(FIELD_TYPES) + "."},
           "default": {"description": "Optional default value (any JSON; coerced "
                                      "to the field type)."},
           "required": {"type": "boolean",
                        "description": "Whether the field is required."}},
          ["type", "name", "field_type"]),
    _tool("drop_field",
          "Remove a field from a type. Existing values are hidden, not destroyed "
          "(re-add the field to recover them).",
          {**_APP, "type": {"type": "string"}, "name": {"type": "string"}},
          ["type", "name"]),
    _tool("add_relation",
          "Declare a relation on a type (links to another type). Declared ONCE on "
          "the 'from' side; the inverse appears automatically on the other type. "
          "Cardinality X_to_Y: the from-side sees Y, the to-side sees X. Use "
          "symmetric:true for a mutual self-link (e.g. friends; to == this type, "
          "one_to_one or many_to_many).",
          {**_APP,
           "type": {"type": "string", "description": "The 'from' type."},
           "name": {"type": "string", "description": "Relation name on this type."},
           "to": {"type": "string", "description": "The target type."},
           "cardinality": {"type": "string", "enum": CARDINALITIES,
                           "description": "One of: " + ", ".join(CARDINALITIES) + "."},
           "inverse": {"type": "string",
                       "description": "Name the other side sees (required unless "
                                      "symmetric)."},
           "symmetric": {"type": "boolean",
                         "description": "Mutual self-link with one shared label."},
           "description": {"type": "string"},
           "inverse_description": {"type": "string"}},
          ["type", "name", "to", "cardinality"]),
    _tool("drop_relation",
          "Remove a relation (and its edges) from the type that authored it. Drop "
          "from the authoring side, not the inverse side.",
          {**_APP, "type": {"type": "string"}, "name": {"type": "string"}},
          ["type", "name"]),
    _tool("delete_type",
          "Delete a type, its objects, and the edges touching them. Neighbor "
          "objects of OTHER types survive.",
          {**_APP, "type": {"type": "string"}},
          ["type"]),
    _tool("set_schema",
          "Escape hatch: apply a raw schema document to a type for anything the "
          "other tools don't cover. doc = {fields?, relations?, merge?}. "
          "merge:true adds without dropping; merge:false replaces.",
          {**_APP,
           "type": {"type": "string"},
           "doc": {"type": "object",
                   "description": "Schema document {fields?, relations?, merge?}."}},
          ["type", "doc"]),
    _tool("query_objects",
          "Read-only: list/query objects of a type for DEBUGGING (inspect data the "
          "site wrote). The frontend should call the HTTP object endpoints itself; "
          "this is for you to peek. query is a raw query string, e.g. "
          "'done=false&sort=priority&order=desc&limit=20'.",
          {**_APP,
           "type": {"type": "string"},
           "query": {"type": "string",
                     "description": "Optional URL query string of filters."}},
          ["type"]),
]

HANDLERS = {
    "register_app": _t_register_app,
    "delete_app": _t_delete_app,
    "list_types": _t_list_types,
    "describe_type": _t_describe_type,
    "add_field": _t_add_field,
    "drop_field": _t_drop_field,
    "add_relation": _t_add_relation,
    "drop_relation": _t_drop_relation,
    "delete_type": _t_delete_type,
    "set_schema": _t_set_schema,
    "query_objects": _t_query_objects,
}


# --- JSON-RPC dispatch --------------------------------------------------------


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(mid, params):
    if not isinstance(params, dict):
        return _result(mid, _tool_result("Error: params must be an object.", True))
    name = params.get("name")
    args = params.get("arguments") or {}
    handler = HANDLERS.get(name)
    if handler is None:
        return _result(mid, _tool_result(
            "Error: unknown tool '{}'.".format(name), True))
    if not isinstance(args, dict):
        return _result(mid, _tool_result(
            "Error: 'arguments' must be an object.", True))
    try:
        out = handler(args)
    except _ApiError as e:
        return _result(mid, _tool_result("Error: " + str(e), True))
    except Exception as e:               # defensive: never crash on a tool bug
        return _result(mid, _tool_result("Error: internal: {}".format(e), True))
    if out is None:
        text = "Done."
    elif isinstance(out, str):
        text = out
    else:
        text = json.dumps(out, indent=2, sort_keys=False)
    return _result(mid, _tool_result(text))


def handle(msg):
    """Process one JSON-RPC message. Return a response dict, or ``None`` for
    notifications (messages with no ``id``), which get no reply."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    method = msg.get("method")
    is_notification = "id" not in msg

    if method == "initialize":
        params = msg.get("params") or {}
        return _result(mid, {
            "protocolVersion": params.get("protocolVersion", _DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "morphdb", "version": __version__},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        return _call_tool(mid, msg.get("params") or {})

    if is_notification:
        return None                       # ignore unknown notifications
    return _error(mid, -32601, "Method not found: {}".format(method))


def _write(stdout, obj):
    # json.dumps escapes any embedded newlines inside string values, so the line
    # itself never contains a literal newline — preserving the stdio framing.
    stdout.write(json.dumps(obj) + "\n")
    stdout.flush()


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from ``stdin``, write responses to
    ``stdout``, until EOF (the client closes the pipe)."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    while True:
        line = stdin.readline()
        if not line:                      # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _write(stdout, _error(None, -32700, "Parse error"))
            continue
        resp = handle(msg)
        if resp is not None:
            _write(stdout, resp)
    return 0


# --- best-effort registration check (for `morphdb status`) --------------------


def _contains_mcp_key(node, key):
    """True if any ``mcpServers`` object anywhere in a config has ``key``."""
    if isinstance(node, dict):
        srv = node.get("mcpServers")
        if isinstance(srv, dict) and key in srv:
            return True
        return any(_contains_mcp_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_contains_mcp_key(v, key) for v in node)
    return False


def _config_has_morphdb(path):
    try:
        with open(path) as f:
            return _contains_mcp_key(json.load(f), "morphdb")
    except (OSError, ValueError):
        return False


def registration_summary():
    """A short, human line for ``morphdb status``: is a 'morphdb' MCP server
    registered with Claude Code? Best-effort scan of the usual config files;
    never raises."""
    found = []
    if _config_has_morphdb(os.path.join(os.getcwd(), ".mcp.json")):
        found.append(".mcp.json (project)")
    if _config_has_morphdb(os.path.join(os.path.expanduser("~"), ".claude.json")):
        found.append("~/.claude.json (user)")
    if found:
        return "configured in " + ", ".join(found) + \
               " — Claude Code spawns it over stdio (not a managed daemon)"
    return ("available — register with `claude mcp add morphdb -- morphdb mcp` "
            "(Claude Code spawns it over stdio; it is not a managed daemon)")


def main(argv=None):
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
