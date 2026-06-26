"""``morphdb app`` / ``schema`` / ``query`` / ``export-schema`` / ``init``
— the data-model CLI.

The coding agent reshapes an app's data model through these subcommands instead
of hand-writing curl against the schema endpoints. They are a thin, zero-dependency
HTTP client of the running MorphDB backend (the same daemon ``morphdb start``
runs), so there is always exactly one writer.

Multi-tenancy: one MorphDB instance hosts many apps (one per website). Register an
app once with a key you pick, remember it, and pass it to every schema/query
command via ``--app`` or ``$MORPHDB_APP`` — it rides on the ``X-App-Key`` header.
There is no way to list apps back, so don't lose the key.

(Reading/writing object *data* at runtime is the frontend's job — it calls
``/objects/...`` over HTTP directly. ``morphdb query`` is only a read-only peek for
you to debug what the site wrote.)

Host defaults to ``http://127.0.0.1:8787``; override with ``$MORPHDB_HOST`` (a full
URL, or a bare ``host[:port]`` which assumes http) or ``--url``. This module owns no
argparse top-level: :func:`add_commands` grafts its subparsers onto the ``morphdb``
CLI.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from ..associations import CARDINALITIES as _CARDINALITIES
from ..fieldtypes import FIELD_TYPES as _FIELD_TYPES

# Ordered lists for argparse `choices`; the canonical sets live in the engine.
FIELD_TYPES = sorted(_FIELD_TYPES)
CARDINALITIES = sorted(_CARDINALITIES)

# The portable app-schema file convention: committed at a website's repo root,
# `morphdb init` reads it to stand the app up on any backend.
DEFAULT_SCHEMA_FILE = "morphdb.schema.json"


def _default_base():
    """Where MorphDB is hosted: ``$MORPHDB_HOST``, else localhost:8787.

    Accepts a full URL ("https://db.example.com") or a bare host[:port]
    ("192.168.1.5:8787"), in which case http:// is assumed.
    """
    host = os.environ.get("MORPHDB_HOST", "").strip()
    if not host:
        return "http://127.0.0.1:8787"
    return host if "://" in host else "http://" + host


def _request(url, method, path, body=None, app=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if app:
        req.add_header("X-App-Key", app)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            msg = raw.decode(errors="replace") or e.reason
        sys.exit(f"error {e.code}: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach MorphDB at {url} ({e.reason}). Is the server "
                 "running? Try `morphdb start`, or set MORPHDB_HOST to a hosted one.")


def _app_exists(url, app):
    """True if ``app`` is registered. Probes GET /schema, which 404s with an
    'Unknown app' error when the key isn't registered (there is no 'get app' route)."""
    req = urllib.request.Request(url.rstrip("/") + "/schema", method="GET")
    req.add_header("X-App-Key", app)
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        sys.exit(f"error {e.code}: could not check whether app '{app}' exists.")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach MorphDB at {url} ({e.reason}). Is the server running?")


def _resolve_app(args):
    """The app key for a scoped command: ``--app``, else ``$MORPHDB_APP``."""
    app = (getattr(args, "app", None) or os.environ.get("MORPHDB_APP") or "").strip()
    if not app:
        sys.exit("error: no app key. Register one with `morphdb app register <key>`, "
                 "then pass --app <key> or export MORPHDB_APP=<key>.")
    return app


def _get_type(url, app, type_name):
    return _request(url, "GET", f"/schema/{type_name}", app=app)


def _put_type(url, app, type_name, doc):
    return _request(url, "PUT", f"/schema/{type_name}", doc, app=app)


def _pretty(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


def _authoring_def(view):
    """Strip a relation's read-back doc down to the fields a PUT accepts (so the
    surviving relations can be re-authored when one is dropped)."""
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


def _slim_field(fdef):
    """Compact a normalized field def for the export file. Drop default flags
    (``required``/``index`` when false) and collapse to a bare type string when
    only the type remains, so the JSON reads like a human wrote it. ``default`` is
    kept whenever it is not null — false/0/"" are real defaults, not noise."""
    out = {"type": fdef["type"]}
    if fdef.get("required"):
        out["required"] = True
    if fdef.get("default") is not None:
        out["default"] = fdef["default"]
    if fdef.get("index"):
        out["index"] = True
    return out if len(out) > 1 else fdef["type"]


def _confirm(prompt):
    """Interactive y/N prompt; False on EOF (no input stream)."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _parse_default(raw):
    """Interpret --default as JSON if possible, else as a literal string.

    The server coerces to the field's type, so "5" / 5 both work for a number
    field; this just lets booleans/numbers/json pass through cleanly.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


# --- commands -----------------------------------------------------------------


def cmd_register_app(args):
    _pretty(_request(args.url, "POST", "/app", {"key": args.key}))
    print(f"\nApp '{args.key}' registered. Use it on every schema/query command:\n"
          f"  export MORPHDB_APP={args.key}\n"
          f"  (frontend: send it as the 'X-App-Key' header)", file=sys.stderr)
    return 0


def cmd_delete_app(args):
    _pretty(_request(args.url, "DELETE", f"/app/{args.key}"))
    return 0


def cmd_list(args):
    _pretty(_request(args.url, "GET", "/schema", app=_resolve_app(args)))
    return 0


def cmd_show(args):
    _pretty(_get_type(args.url, _resolve_app(args), args.type))
    return 0


def cmd_add_field(args):
    fdef = {"type": args.ftype}
    if args.default is not None:
        fdef["default"] = _parse_default(args.default)
    if args.required:
        fdef["required"] = True
    if args.index:
        fdef["index"] = True
    # merge:true so existing fields/relations are untouched (idempotent re-runs).
    doc = {"merge": True, "fields": {args.name: fdef}}
    _pretty(_put_type(args.url, _resolve_app(args), args.type, doc))
    return 0


def cmd_drop_field(args):
    app = _resolve_app(args)
    current = _get_type(args.url, app, args.type)
    fields = current.get("fields", {})
    if args.name not in fields:
        sys.exit(f"error: type '{args.type}' has no field '{args.name}'.")
    fields.pop(args.name)
    # Replace fields (merge:false) with the remainder; omit 'relations' so they
    # are left untouched.
    _pretty(_put_type(args.url, app, args.type, {"merge": False, "fields": fields}))
    return 0


def cmd_add_relation(args):
    rel = {"to": args.to, "cardinality": args.cardinality}
    if args.symmetric:
        rel["symmetric"] = True
    elif args.inverse:
        rel["inverse"] = args.inverse
    else:
        sys.exit("error: a non-symmetric relation needs --inverse (the name the "
                 "other side sees), or pass --symmetric.")
    if args.description:
        rel["description"] = args.description
    if args.inverse_description:
        rel["inverse_description"] = args.inverse_description
    doc = {"merge": True, "relations": {args.name: rel}}
    _pretty(_put_type(args.url, _resolve_app(args), args.type, doc))
    return 0


def cmd_drop_relation(args):
    app = _resolve_app(args)
    current = _get_type(args.url, app, args.type)
    relations = current.get("relations", {})
    if args.name not in relations:
        inverse = current.get("inverse_relations", {})
        if args.name in inverse:
            via = inverse[args.name]
            sys.exit(
                f"error: '{args.name}' is an inverse relation, authored on type "
                f"'{via.get('via_type')}' as '{via.get('via_relation')}'. Drop it "
                "from that side.")
        sys.exit(f"error: type '{args.type}' has no relation '{args.name}'.")
    # Re-author the remaining relations (their defs are valid authoring docs) and
    # replace (merge:false) so the named one is pruned along with its edges.
    relations.pop(args.name)
    remaining = {k: _authoring_def(v) for k, v in relations.items()}
    _pretty(_put_type(args.url, app, args.type, {"merge": False, "relations": remaining}))
    return 0


def cmd_delete_type(args):
    _pretty(_request(args.url, "DELETE", f"/schema/{args.type}", app=_resolve_app(args)))
    return 0


def cmd_set(args):
    try:
        doc = json.loads(args.json)
    except ValueError as e:
        sys.exit(f"error: --json is not valid JSON ({e}).")
    _pretty(_put_type(args.url, _resolve_app(args), args.type, doc))
    return 0


def cmd_query(args):
    """Read-only peek at object data, for debugging (the frontend reads it itself)."""
    app = _resolve_app(args)
    path = "/objects/" + args.type
    if args.query:
        path += "?" + args.query.lstrip("?")
    _pretty(_request(args.url, "GET", path, app=app))
    return 0


def cmd_export_schema(args):
    """Dump an app's whole data model as portable JSON (on stdout). Lets someone
    cloning a MorphDB-backed repo rebuild the schema on their own instance."""
    app = args.app_name
    types = _request(args.url, "GET", "/schema", app=app)["types"]
    payload = {
        # ponytail: a format tag so a future reconstruct can recognize old files.
        # Only one format exists, so it is written but not yet validated.
        "morphdb_schema_version": 1,
        "app": app,
        "types": [
            {
                "name": t["name"],
                "fields": {k: _slim_field(v) for k, v in t.get("fields", {}).items()},
                "relations": {k: _authoring_def(v)
                              for k, v in t.get("relations", {}).items()},
            }
            for t in types
        ],
    }
    _pretty(payload)
    return 0


def _apply_schema(url, app, types):
    """Apply an export file's types to an app, additively (merge). Two passes:
    every type's fields first — so a relation's target type already exists by the
    time the relation is declared — then the relations."""
    for t in types:
        _put_type(url, app, t["name"], {"merge": True, "fields": t.get("fields") or {}})
    for t in types:
        rels = t.get("relations") or {}
        if rels:
            _put_type(url, app, t["name"], {"merge": True, "relations": rels})


def cmd_init(args):
    """Stand an app up from its schema file (default ``./morphdb.schema.json``),
    idempotently. The app key lives in the file.

    - App missing  -> create it + apply the schema           (status "created")
    - App exists   -> merge the schema additively, keep data  (status "merged")
                      — a name clash NEVER deletes anything.
    - ``--reset``  -> the only destructive path: delete the app and rebuild it
                      clean (status "reset"). Interactive runs confirm first.
    """
    try:
        with open(args.file) as f:
            doc = json.load(f)
    except FileNotFoundError:
        hint = (f" Run `morphdb export-schema <app> > {args.file}` to create one."
                if args.file == DEFAULT_SCHEMA_FILE else "")
        sys.exit(f"error: schema file '{args.file}' not found.{hint}")
    except (OSError, ValueError) as e:
        sys.exit(f"error: cannot read schema file '{args.file}': {e}")
    if not isinstance(doc, dict) or "app" not in doc or "types" not in doc:
        sys.exit("error: not a MorphDB schema export (expected 'app' and 'types' keys).")
    app, types = doc["app"], doc["types"]

    exists = _app_exists(args.url, app)
    did_reset = False
    if exists and args.reset:
        # Only --reset deletes. Confirm interactively; the flag itself is the
        # confirmation when there's no tty (e.g. an agent), so never hang.
        if sys.stdin.isatty() and not _confirm(
                f"Overwrite app '{app}'? Deletes its schema and ALL objects. [y/N] "):
            sys.exit("Aborted; nothing changed.")
        _request(args.url, "DELETE", f"/app/{app}")
        exists, did_reset = False, True

    if exists:
        # Name clash: keep the existing app and its data, merge the schema in.
        print(f"app '{app}' already exists — merging schema additively; existing "
              "data kept. Use --reset to rebuild it clean.", file=sys.stderr)
        _apply_schema(args.url, app, types)
        status = "merged"
    else:
        _request(args.url, "POST", "/app", {"key": app})
        _apply_schema(args.url, app, types)
        status = "reset" if did_reset else "created"

    _pretty({"app": app, "status": status, "types": [t["name"] for t in types]})
    return 0


# --- parser wiring ------------------------------------------------------------


def add_commands(sub):
    """Graft the ``app`` / ``schema`` / ``query`` command groups onto the
    ``morphdb`` CLI's subparsers object (``sub``)."""
    url_only = argparse.ArgumentParser(add_help=False)
    url_only.add_argument("--url", default=_default_base(),
                          help="MorphDB base URL (default $MORPHDB_HOST or "
                               "http://127.0.0.1:8787)")
    # Most commands also take --app; export/init get the app name elsewhere
    # (a positional / from the file), so they reuse url_only without an --app flag.
    common = argparse.ArgumentParser(add_help=False, parents=[url_only])
    common.add_argument("--app", default=None,
                        help="app key (default $MORPHDB_APP); sent as X-App-Key")

    # morphdb app <register|delete>
    app_p = sub.add_parser("app", help="register or delete apps (tenants)")
    app_sub = app_p.add_subparsers(dest="app_command", required=True)
    sp = app_sub.add_parser("register", parents=[common],
                            help="register a new app under a key you choose")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_register_app)
    sp = app_sub.add_parser("delete", parents=[common],
                            help="delete an app and cascade-delete everything under it")
    sp.add_argument("key")
    sp.set_defaults(func=cmd_delete_app)

    # morphdb schema <list|show|add-field|...>
    schema_p = sub.add_parser("schema", help="inspect and edit an app's data model")
    schema_sub = schema_p.add_subparsers(dest="schema_command", required=True)

    schema_sub.add_parser("list", parents=[common],
                          help="show every type (fields + relations)"
                          ).set_defaults(func=cmd_list)

    sp = schema_sub.add_parser("show", parents=[common], help="show one type's schema")
    sp.add_argument("type")
    sp.set_defaults(func=cmd_show)

    sp = schema_sub.add_parser("add-field", parents=[common],
                               help="add/update a field (idempotent; O(1))")
    sp.add_argument("type")
    sp.add_argument("name")
    sp.add_argument("ftype", choices=FIELD_TYPES)
    sp.add_argument("--default")
    sp.add_argument("--required", action="store_true")
    sp.add_argument("--index", action="store_true",
                    help="make this field filterable/sortable (backfills existing "
                         "objects; json can't be indexed)")
    sp.set_defaults(func=cmd_add_field)

    sp = schema_sub.add_parser("drop-field", parents=[common],
                               help="remove a field (values hidden, not destroyed)")
    sp.add_argument("type")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_drop_field)

    sp = schema_sub.add_parser("add-relation", parents=[common],
                               help="declare a relation (inverse appears automatically)")
    sp.add_argument("type")
    sp.add_argument("name")
    sp.add_argument("--to", required=True)
    sp.add_argument("--cardinality", required=True, choices=CARDINALITIES)
    sp.add_argument("--inverse")
    sp.add_argument("--symmetric", action="store_true")
    sp.add_argument("--description")
    sp.add_argument("--inverse-description", dest="inverse_description")
    sp.set_defaults(func=cmd_add_relation)

    sp = schema_sub.add_parser("drop-relation", parents=[common],
                               help="remove a relation + its edges (from the authoring side)")
    sp.add_argument("type")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_drop_relation)

    sp = schema_sub.add_parser("delete-type", parents=[common],
                               help="delete a type, its objects, and their edges")
    sp.add_argument("type")
    sp.set_defaults(func=cmd_delete_type)

    sp = schema_sub.add_parser("set", parents=[common],
                               help="escape hatch: PUT a raw schema document")
    sp.add_argument("type")
    sp.add_argument("--json", required=True,
                    help='a schema doc, e.g. \'{"merge":true,"fields":{"due":"datetime"}}\'')
    sp.set_defaults(func=cmd_set)

    # morphdb query <type> [querystring]
    sp = sub.add_parser("query", parents=[common],
                        help="read objects of a type for debugging (the frontend "
                             "reads data itself)")
    sp.add_argument("type")
    sp.add_argument("query", nargs="?", default=None,
                    help="optional URL query string: filters on indexed fields, "
                         "sort, limit/offset, include — e.g. "
                         "'done=false&sort=priority&order=desc&limit=20'")
    sp.set_defaults(func=cmd_query)

    # morphdb export-schema <app>  /  init <file> — move an app's whole data model
    # between instances. export-schema snapshots a schema to a portable file;
    # init stands an app up from that file on any backend.
    sp = sub.add_parser("export-schema", parents=[url_only],
                        help="export an app's schema as JSON on stdout (redirect to "
                             "a file to commit/share it)")
    sp.add_argument("app_name", metavar="app", help="the app key to export")
    sp.set_defaults(func=cmd_export_schema)

    sp = sub.add_parser("init", parents=[url_only],
                        help="stand an app up from its schema file (idempotent: "
                             "merges into an existing app, never deletes)")
    sp.add_argument("file", nargs="?", default=DEFAULT_SCHEMA_FILE,
                    help=f"a JSON file from `morphdb export-schema` "
                         f"(default ./{DEFAULT_SCHEMA_FILE})")
    sp.add_argument("--reset", action="store_true",
                    help="if the app already exists, delete and rebuild it clean "
                         "(destroys its current schema and objects)")
    sp.set_defaults(func=cmd_init)
