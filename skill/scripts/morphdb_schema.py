#!/usr/bin/env python3
"""morphdb_schema — a tiny CLI for editing a MorphDB schema.

The coding agent reshapes the data model through this script instead of
hand-writing curl against the schema endpoints. It is a thin, zero-dependency
wrapper over `GET/PUT/DELETE /schema[/{type}]` on a running MorphDB server.

(Reading and writing actual object *data* is intentionally not here — the
frontend you build will call `/objects/...` over HTTP directly, so use curl for
those while developing.)

Usage:
    python3 morphdb_schema.py [--url URL] <command> ...

    list                          Show every type (fields + relations).
    show   <type>                 Show one type's schema.

    add-field    <type> <name> <ftype> [--default V] [--required]
    drop-field   <type> <name>

    add-relation <type> <name> --to T --cardinality C
                 [--inverse I] [--symmetric]
                 [--description D] [--inverse-description ID]
    drop-relation <type> <name>

    delete-type  <type>
    set          <type> --json '{"fields":{...},"relations":{...},"merge":true}'

URL defaults to $MORPHDB_URL or http://127.0.0.1:8787.

Field types: string, number, boolean, json, datetime.
Cardinalities: one_to_one, one_to_many, many_to_one, many_to_many.
A relation is declared once (on the "from" side); its inverse appears
automatically on the other type. Use --symmetric for mutual links within one
type (to == <type>, cardinality one_to_one or many_to_many).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("MORPHDB_URL", "http://127.0.0.1:8787")


def _request(url, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
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
        sys.exit(f"cannot reach MorphDB at {url} ({e.reason}). Is the server running?")


def _get_type(url, type_name):
    return _request(url, "GET", f"/schema/{type_name}")


def _put_type(url, type_name, doc):
    return _request(url, "PUT", f"/schema/{type_name}", doc)


def _pretty(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


# --- commands -----------------------------------------------------------------


def cmd_list(url, args):
    _pretty(_request(url, "GET", "/schema"))


def cmd_show(url, args):
    _pretty(_get_type(url, args.type))


def _parse_default(raw):
    """Interpret --default as JSON if possible, else as a literal string.

    The server coerces the value to the field's type, so "5" / 5 both work for a
    number field; this just lets booleans/numbers/json pass through cleanly.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def cmd_add_field(url, args):
    fdef = {"type": args.ftype}
    if args.default is not None:
        fdef["default"] = _parse_default(args.default)
    if args.required:
        fdef["required"] = True
    # merge:true so existing fields/relations are untouched.
    doc = {"merge": True, "fields": {args.name: fdef}}
    _pretty(_put_type(url, args.type, doc))


def cmd_drop_field(url, args):
    current = _get_type(url, args.type)
    fields = current.get("fields", {})
    if args.name not in fields:
        sys.exit(f"error: type '{args.type}' has no field '{args.name}'.")
    fields.pop(args.name)
    # Replace fields (merge:false) with the remaining set; omit 'relations' so
    # they are left untouched.
    _pretty(_put_type(url, args.type, {"merge": False, "fields": fields}))


def cmd_add_relation(url, args):
    rel = {"to": args.to, "cardinality": args.cardinality}
    if args.symmetric:
        rel["symmetric"] = True
    elif args.inverse:
        rel["inverse"] = args.inverse
    else:
        sys.exit("error: a non-symmetric relation needs --inverse (the name the "
                 "other side sees).")
    if args.description:
        rel["description"] = args.description
    if args.inverse_description:
        rel["inverse_description"] = args.inverse_description
    doc = {"merge": True, "relations": {args.name: rel}}
    _pretty(_put_type(url, args.type, doc))


def cmd_drop_relation(url, args):
    current = _get_type(url, args.type)
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
    _pretty(_put_type(url, args.type, {"merge": False, "relations": remaining}))


def _authoring_def(view):
    """Strip a relation's read-back doc down to the fields PUT accepts."""
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


def cmd_delete_type(url, args):
    _pretty(_request(url, "DELETE", f"/schema/{args.type}"))


def cmd_set(url, args):
    try:
        doc = json.loads(args.json)
    except ValueError as e:
        sys.exit(f"error: --json is not valid JSON ({e}).")
    _pretty(_put_type(url, args.type, doc))


def build_parser():
    p = argparse.ArgumentParser(prog="morphdb_schema", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL, help=f"MorphDB base URL (default {DEFAULT_URL})")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    sp = sub.add_parser("show"); sp.add_argument("type"); sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("add-field")
    sp.add_argument("type"); sp.add_argument("name")
    sp.add_argument("ftype", choices=["string", "number", "boolean", "json", "datetime"])
    sp.add_argument("--default"); sp.add_argument("--required", action="store_true")
    sp.set_defaults(func=cmd_add_field)

    sp = sub.add_parser("drop-field")
    sp.add_argument("type"); sp.add_argument("name"); sp.set_defaults(func=cmd_drop_field)

    sp = sub.add_parser("add-relation")
    sp.add_argument("type"); sp.add_argument("name")
    sp.add_argument("--to", required=True)
    sp.add_argument("--cardinality", required=True,
                    choices=["one_to_one", "one_to_many", "many_to_one", "many_to_many"])
    sp.add_argument("--inverse")
    sp.add_argument("--symmetric", action="store_true")
    sp.add_argument("--description")
    sp.add_argument("--inverse-description", dest="inverse_description")
    sp.set_defaults(func=cmd_add_relation)

    sp = sub.add_parser("drop-relation")
    sp.add_argument("type"); sp.add_argument("name"); sp.set_defaults(func=cmd_drop_relation)

    sp = sub.add_parser("delete-type")
    sp.add_argument("type"); sp.set_defaults(func=cmd_delete_type)

    sp = sub.add_parser("set")
    sp.add_argument("type"); sp.add_argument("--json", required=True)
    sp.set_defaults(func=cmd_set)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args.url, args)


if __name__ == "__main__":
    main()
