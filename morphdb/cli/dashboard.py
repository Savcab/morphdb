"""Read-only admin dashboard: every app and its data model, in one local page.

Operator-facing and local-only — it opens the database directly (through the
storage backend, SQLite or Postgres) rather than going through the HTTP API, so
it can list apps without adding a "list apps" endpoint to the public surface
(which is intentionally absent).

The page has two tabs: a **Data model** view (each app's types as an interactive
ER-style graph — type nodes + crow's-foot relation edges — plus a complete table)
and a **Tables · raw** view (every underlying table with its columns and rows,
capped per table). The graph and modals are progressive enhancement over
server-rendered HTML, with zero external dependencies (inline CSS/SVG/JS only).
"""

import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import backend as _backend
from ..storage import DynamoStorage


def gather(target):
    """A read-only snapshot of every app and its data model::

        {apps: [{
            app,
            types:     [{name, fields: [{name, type}], count}],
            relations: [{name, from, to, forward, inverse, cardinality, symmetric}],
            edges:     <int edge count>,
        }]}

    Reads through the storage backend (SQLite or Postgres). Tolerates a
    missing/empty/unreachable database by returning an ``error`` string.
    """
    try:
        be = _backend.from_target(target)
        raw = be.connect()
    except Exception as e:
        return {"error": f"cannot open database: {e}", "apps": []}
    if be.name == "dynamodb":
        try:
            return _gather_storage(DynamoStorage(raw))
        except Exception as e:
            return {"error": f"cannot read DynamoDB table: {e}", "apps": []}
        finally:
            raw.close()
    # A private lock: this is a separate, short-lived inspection connection.
    c = _backend.Connection(be, raw, threading.RLock())
    try:
        try:
            apps = [r["key"] for r in c.execute("SELECT key FROM apps ORDER BY key")]
        except Exception:
            return {"error": "no MorphDB schema in this database yet", "apps": []}
        out = []
        for app in apps:
            types = []
            for r in c.execute(
                    "SELECT name, fields FROM object_schemas WHERE app=? ORDER BY name",
                    (app,)):
                fields = []
                try:
                    for fname, fdef in json.loads(r["fields"]).items():
                        ftype = fdef.get("type", "?") if isinstance(fdef, dict) else str(fdef)
                        fields.append({"name": fname, "type": ftype})
                except Exception:
                    pass
                count = c.execute(
                    "SELECT COUNT(*) AS n FROM objects WHERE app=? AND object_type=?",
                    (app, r["name"])).fetchone()["n"]
                types.append({"name": r["name"], "fields": fields, "count": count})
            relations = [
                {"name": r["name"], "from": r["from_type"], "to": r["to_type"],
                 "forward": r["forward_name"], "inverse": r["inverse_name"],
                 "cardinality": r["cardinality"], "symmetric": bool(r["symmetric"])}
                for r in c.execute(
                    "SELECT name, from_type, to_type, forward_name, inverse_name, "
                    "cardinality, \"symmetric\" FROM association_schemas WHERE app=? "
                    "ORDER BY name", (app,))]
            edges = c.execute(
                "SELECT COUNT(*) AS n FROM associations WHERE app=?", (app,)).fetchone()["n"]
            out.append({"app": app, "types": types,
                        "relations": relations, "edges": edges})
        return {"apps": out, "tables": _gather_tables(c, be, raw)}
    finally:
        raw.close()


def _gather_storage(s):
    """Dashboard snapshot through the logical storage facade.

    Used by DynamoDB, which has MorphDB concepts but no SQL tables to query.
    The raw table explorer remains SQL-specific; for DynamoDB we expose a compact
    logical view instead.
    """
    out = []
    for app_row in s.list_apps():
        app = app_row["key"]
        types = []
        for r in s.list_object_schemas(app):
            fields = []
            try:
                for fname, fdef in json.loads(r["fields"]).items():
                    ftype = fdef.get("type", "?") if isinstance(fdef, dict) else str(fdef)
                    fields.append({"name": fname, "type": ftype})
            except Exception:
                pass
            types.append({
                "name": r["name"],
                "fields": fields,
                "count": len(s.list_objects(app, r["name"])),
            })
        rel_rows = s.list_association_schemas(app)
        relations = [
            {"name": r["name"], "from": r["from_type"], "to": r["to_type"],
             "forward": r["forward_name"], "inverse": r["inverse_name"],
             "cardinality": r["cardinality"], "symmetric": bool(r["symmetric"])}
            for r in sorted(rel_rows, key=lambda row: row["name"])
        ]
        edges = sum(len(s.list_edges(app, r["name"])) for r in rel_rows)
        out.append({"app": app, "types": types, "relations": relations, "edges": edges})
    return {"apps": out, "tables": _gather_logical_tables(s, out)}


def _gather_logical_tables(s, apps, cap=250):
    rows_by_name = {"apps": [], "object_schemas": [], "objects": [],
                    "association_schemas": [], "associations": []}
    for app in apps:
        key = app["app"]
        rows_by_name["apps"].append([key])
        for t in s.list_object_schemas(key):
            rows_by_name["object_schemas"].append([
                t["app"], t["name"], t["fields"], t["created_at"], t["updated_at"]])
            for obj in s.list_objects(key, t["name"]):
                rows_by_name["objects"].append([
                    obj["guid"], obj["app"], obj["object_type"], obj["data"],
                    obj["created_at"], obj["updated_at"]])
        for rel in s.list_association_schemas(key):
            rows_by_name["association_schemas"].append([
                rel["app"], rel["name"], rel["from_type"], rel["to_type"],
                rel["forward_name"], rel["inverse_name"], rel["cardinality"],
                rel["symmetric"], rel["created_at"], rel["updated_at"]])
            for edge in s.list_edges(key, rel["name"]):
                rows_by_name["associations"].append([
                    edge["id"], key, rel["name"], edge["from_guid"],
                    edge["to_guid"], edge["created_at"]])
    cols = {
        "apps": ["key"],
        "object_schemas": ["app", "name", "fields", "created_at", "updated_at"],
        "objects": ["guid", "app", "object_type", "data", "created_at", "updated_at"],
        "association_schemas": [
            "app", "name", "from_type", "to_type", "forward_name",
            "inverse_name", "cardinality", "symmetric", "created_at", "updated_at",
        ],
        "associations": ["id", "app", "assoc_name", "from_guid", "to_guid", "created_at"],
    }
    tables = []
    for name in _TABLE_ORDER:
        if name == "field_index":
            continue
        rows = rows_by_name[name]
        tables.append({"name": f"{name} (logical)", "columns": cols[name],
                       "rows": rows[:cap], "total": len(rows),
                       "shown": min(len(rows), cap)})
    return tables


# Per-table row cap for the raw "Tables" explorer — keeps the generated page a
# sane size even when a type has tens of thousands of objects. The total is still
# reported, so a truncated table reads as "first N of TOTAL", never as "all".
_ROW_CAP = 250

# Show the logical tables in dependency order (tenant root first), then anything
# else the backend reports, so the explorer reads top-down like the data model.
_TABLE_ORDER = ["apps", "object_schemas", "objects", "field_index",
                "association_schemas", "associations"]


def _gather_tables(c, be, raw, cap=_ROW_CAP):
    """Every real table in the database with its columns and (capped) rows::

        [{name, columns: [str], rows: [[cell, ...]], total: int, shown: int}]

    Lists tables and columns through the backend, so it works on SQLite and
    Postgres alike.
    """
    names = be.list_tables(raw)
    names.sort(key=lambda n: (_TABLE_ORDER.index(n) if n in _TABLE_ORDER
                              else len(_TABLE_ORDER), n))
    tables = []
    for name in names:
        cols = be.table_columns(raw, name)
        total = c.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
        rows = [[r[col] for col in cols]
                for r in c.execute(f'SELECT * FROM "{name}" LIMIT {int(cap)}')]
        tables.append({"name": name, "columns": cols, "rows": rows,
                       "total": total, "shown": len(rows)})
    return tables


# --- presentation -------------------------------------------------------------

_CSS = r"""
*, *::before, *::after { box-sizing: border-box; }
:root {
  --ground:#0E1320; --panel:#151B2B; --node:#1B2336; --node-hi:#222C44;
  --line:#2A3550; --line-soft:#212A40;
  --ink:#E6ECF7; --dim:#8593AD; --faint:#56627E;
  --accent:#5FD3C4; --accent-dim:#2F6E68; --violet:#C9A0FF; --amber:#E8B864;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
html { -webkit-text-size-adjust:100%; }
body {
  margin:0; color:var(--ink); font:15px/1.55 var(--sans);
  background:
    linear-gradient(rgba(123,148,196,.045) 1px, transparent 1px) 0 0/100% 30px,
    linear-gradient(90deg, rgba(123,148,196,.045) 1px, transparent 1px) 0 0/30px 100%,
    radial-gradient(1200px 600px at 75% -8%, rgba(95,211,196,.07), transparent 60%),
    var(--ground);
  min-height:100vh;
}
.wrap { max-width:1120px; margin:0 auto; padding:36px 22px 80px; }

.top { display:flex; align-items:flex-end; justify-content:space-between;
       gap:18px; flex-wrap:wrap; padding-bottom:16px;
       border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:baseline; gap:10px; }
.brand b { font:600 21px/1 var(--mono); letter-spacing:-.02em; }
.brand .slash { color:var(--accent); font:600 21px/1 var(--mono); }
.brand .sub { color:var(--dim); font:13px/1 var(--mono); letter-spacing:.18em;
              text-transform:uppercase; }
.dbpath { color:var(--faint); font:12px/1.4 var(--mono); word-break:break-all;
          text-align:right; max-width:46ch; }

.summary { display:flex; gap:10px; flex-wrap:wrap; margin:20px 0 26px; }
.stat { background:var(--panel); border:1px solid var(--line);
        border-radius:10px; padding:10px 15px; min-width:96px; }
.stat .num { font:600 22px/1 var(--mono); font-variant-numeric:tabular-nums;
             letter-spacing:-.02em; }
.stat .lab { color:var(--dim); font:11px/1 var(--sans); text-transform:uppercase;
             letter-spacing:.09em; margin-top:7px; }

.app { background:var(--panel); border:1px solid var(--line); border-radius:14px;
       margin-bottom:16px; overflow:clip; }
.app__head { width:100%; display:flex; align-items:center; gap:14px;
             background:none; border:0; color:inherit; cursor:pointer;
             padding:16px 18px; text-align:left; font:inherit; }
.app__chev { color:var(--faint); transition:transform .22s ease; flex:none;
             font:600 13px/1 var(--mono); }
.app--open .app__chev { transform:rotate(90deg); color:var(--accent); }
.app__key { font:600 17px/1 var(--mono); letter-spacing:-.01em; }
.app__key::before { content:"#"; color:var(--violet); margin-right:1px; }
.app__meta { color:var(--dim); font:12.5px/1 var(--mono); margin-left:auto;
             font-variant-numeric:tabular-nums; white-space:nowrap; }
.app__meta b { color:var(--ink); font-weight:600; }
.app__body { display:none; padding:0 18px 20px; }
.app--open .app__body { display:block; }

.panel { border:1px solid var(--line-soft); border-radius:11px; background:var(--ground);
         margin-bottom:16px; }
.panel__cap { display:flex; align-items:center; justify-content:space-between;
              gap:12px; padding:11px 14px; border-bottom:1px solid var(--line-soft); }
.panel__cap h3 { margin:0; font:600 11px/1 var(--sans); color:var(--dim);
                 letter-spacing:.13em; text-transform:uppercase; }

/* graph */
.graph { position:relative; height:min(620px,72vh); overflow:hidden; }
.graph svg { display:block; width:100%; height:100%; cursor:grab; touch-action:none;
             background:
               linear-gradient(rgba(123,148,196,.05) 1px, transparent 1px) 0 0/26px 26px,
               linear-gradient(90deg, rgba(123,148,196,.05) 1px, transparent 1px) 0 0/26px 26px; }
.graph svg.panning { cursor:grabbing; }
.gtools { position:absolute; top:10px; right:10px; display:flex; gap:6px; z-index:3; }
.gtools button { min-width:30px; height:30px; padding:0 8px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); border-radius:8px; cursor:pointer;
  font:600 14px/1 var(--mono); display:flex; align-items:center; justify-content:center; }
.gtools button:hover { border-color:var(--accent); color:var(--accent); }
.gtools .fit { font-size:11px; letter-spacing:.03em; }
.ghint { position:absolute; left:12px; bottom:10px; z-index:3; pointer-events:none;
  color:var(--faint); font:10.5px/1 var(--mono); }
.gnode { cursor:pointer; }
.gnode rect { fill:var(--node); stroke:var(--line); stroke-width:1.5;
              transition:fill .15s ease, stroke .15s ease; }
.gnode .gn-name { fill:var(--ink); font:600 13px/1 var(--mono); }
.gnode .gn-sub { fill:var(--faint); font:10.5px/1 var(--mono); }
.gnode:hover rect, .gnode:focus-visible rect { fill:var(--node-hi); stroke:var(--accent); }
.gnode:focus { outline:none; }
.gedge { stroke:var(--accent-dim); stroke-width:1.6; fill:none; transition:stroke .15s ease, opacity .15s ease; }
.gfoot { stroke:var(--accent); stroke-width:1.6; fill:none; transition:opacity .15s ease; }
.graph.dim .gedge:not(.lit), .graph.dim .gfoot:not(.lit) { opacity:.12; }
.graph.dim .gnode:not(.lit) { opacity:.3; }
.gedge.lit { stroke:var(--accent); }
.grellabel { fill:var(--dim); font:10px/1 var(--mono); }
.grelname { fill:var(--accent); font:10px/1 var(--mono); }
.grelname.inv { fill:var(--violet); }
.glegend { display:flex; flex-wrap:wrap; gap:15px; padding:11px 14px; align-items:center;
           border-top:1px solid var(--line-soft); }
.glegend .lg-t { color:var(--faint); font:10.5px/1 var(--sans); letter-spacing:.1em;
                 text-transform:uppercase; }
.glegend div { display:flex; align-items:center; gap:7px; color:var(--dim); font:11px/1 var(--mono); }

/* table */
table { width:100%; border-collapse:collapse; }
thead th { text-align:left; padding:9px 14px; color:var(--faint);
           font:600 10.5px/1 var(--sans); letter-spacing:.1em; text-transform:uppercase;
           border-bottom:1px solid var(--line-soft); }
thead th.r { text-align:right; }
.trow { cursor:pointer; }
.trow td { padding:11px 14px; border-bottom:1px solid var(--line-soft);
           vertical-align:middle; }
.trow:last-child td { border-bottom:0; }
.trow:hover td { background:var(--node); }
.trow:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.tname { font:600 14px/1.3 var(--mono); }
.tprev { color:var(--dim); font:12px/1.45 var(--mono); display:-webkit-box;
         -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; max-width:62ch; }
.tprev em { color:var(--faint); font-style:normal; }
.tcount { text-align:right; white-space:nowrap; }
.tcount .n { font:600 14px/1 var(--mono); font-variant-numeric:tabular-nums; }
.bar { height:4px; border-radius:3px; background:var(--accent); margin-top:6px;
       margin-left:auto; max-width:120px; opacity:.65; }
.muted { color:var(--faint); }
.empty, .err { padding:44px 18px; text-align:center; }
.empty { color:var(--dim); } .err { color:#F77; font:14px/1.6 var(--mono); }
.empty code, .err code { font-family:var(--mono); background:var(--node);
        border:1px solid var(--line); padding:2px 7px; border-radius:6px; color:var(--ink); }

footer { color:var(--faint); font:12px/1.5 var(--mono); margin-top:30px;
         text-align:center; }
footer b { color:var(--dim); }

/* modal */
.modal { position:fixed; inset:0; display:none; align-items:center; justify-content:center;
         padding:24px; background:rgba(7,10,18,.72); z-index:50; }
.modal.show { display:flex; }
.sheet { background:var(--panel); border:1px solid var(--line); border-radius:14px;
         width:min(520px,100%); max-height:84vh; overflow:auto;
         box-shadow:0 28px 80px rgba(0,0,0,.55); }
.sheet__top { display:flex; align-items:flex-start; gap:12px; padding:18px 20px 14px;
              border-bottom:1px solid var(--line-soft); position:sticky; top:0;
              background:var(--panel); }
.sheet__title { font:600 18px/1.2 var(--mono); }
.sheet__title::before { content:"type "; color:var(--accent); font-weight:400; font-size:13px; }
.sheet__obj { margin-left:auto; color:var(--dim); font:12px/1.3 var(--mono);
              text-align:right; white-space:nowrap; }
.sheet__obj b { display:block; color:var(--ink); font-size:17px; }
.x { background:none; border:0; color:var(--faint); cursor:pointer; font-size:22px;
     line-height:1; padding:2px 4px; }
.x:hover { color:var(--ink); }
.sheet section { padding:14px 20px; }
.sheet h4 { margin:0 0 10px; font:600 10.5px/1 var(--sans); color:var(--faint);
            letter-spacing:.12em; text-transform:uppercase; }
.frow { display:flex; align-items:baseline; gap:10px; padding:6px 0;
        border-bottom:1px dashed var(--line-soft); }
.frow:last-child { border-bottom:0; }
.frow .fn { font:600 13.5px/1.3 var(--mono); }
.frow .ft { margin-left:auto; color:var(--accent); font:12px/1 var(--mono); }
.rel { padding:8px 0; border-bottom:1px dashed var(--line-soft); font:12.5px/1.5 var(--mono); }
.rel:last-child { border-bottom:0; }
.rel .rn { color:var(--ink); font-weight:600; }
.rel .rc { color:var(--violet); }
.rel .arrow { color:var(--accent); }
.rel small { color:var(--faint); }
.none { color:var(--faint); font:12px/1.4 var(--mono); }

/* tabs */
.tabs { display:flex; gap:2px; margin:6px 0 20px; border-bottom:1px solid var(--line); }
.tab { background:none; border:0; border-bottom:2px solid transparent; color:var(--dim);
       font:600 13px/1 var(--sans); letter-spacing:.01em; padding:11px 16px 12px;
       cursor:pointer; margin-bottom:-1px; }
.tab:hover { color:var(--ink); }
.tab--on { color:var(--accent); border-bottom-color:var(--accent); }
.view { display:none; } .view--on { display:block; }

/* raw table explorer */
.rawtable .panel__cap h3 { font:600 13.5px/1 var(--mono); text-transform:none;
                           letter-spacing:0; color:var(--ink); }
.rawtable .panel__cap h3::before { content:"⊞ "; color:var(--violet); }
.rawnote { color:var(--dim); font:11.5px/1 var(--mono); white-space:nowrap; }
.rawnote b { color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }
.tscroll { overflow:auto; max-height:540px; border-radius:0 0 11px 11px; }
table.raw { min-width:100%; border-collapse:collapse; }
table.raw thead th { position:sticky; top:0; z-index:1; background:var(--node);
  color:var(--dim); font:600 11px/1 var(--mono); text-transform:none; letter-spacing:0;
  text-align:left; white-space:nowrap; padding:9px 13px;
  border-bottom:1px solid var(--line); }
table.raw td { padding:7px 13px; border-bottom:1px solid var(--line-soft);
  font:12px/1.5 var(--mono); color:var(--ink); white-space:nowrap; vertical-align:top;
  max-width:420px; overflow:hidden; text-overflow:ellipsis; }
table.raw tbody tr:hover td { background:var(--node); }
table.raw tbody tr:last-child td { border-bottom:0; }
.raw .null { color:var(--faint); font-style:italic; }
.rawempty { color:var(--faint); text-align:center; padding:22px; }

@media (prefers-reduced-motion: no-preference) {
  .app__body { animation:fade .26s ease; }
  .gedge, .gfoot { stroke-dasharray:1; }
  @keyframes fade { from { opacity:0; transform:translateY(-4px); } to { opacity:1; } }
}
@media (max-width:560px) {
  .app__meta { width:100%; margin:6px 0 0; order:3; }
  .dbpath { text-align:left; }
}
"""


def _esc(s):
    return html.escape(str(s))


def _app_card(a, idx):
    """Server-rendered app card: collapsible head + graph mount + full table."""
    open_cls = " app--open" if idx == 0 else ""
    expanded = "true" if idx == 0 else "false"
    n_types, n_rel = len(a["types"]), len(a["relations"])
    meta = (f"<b>{n_types}</b> type{'s' * (n_types != 1)} · "
            f"<b>{n_rel}</b> relation{'s' * (n_rel != 1)} · "
            f"<b>{a['edges']}</b> edge{'s' * (a['edges'] != 1)}")

    if a["types"]:
        maxc = max((t["count"] for t in a["types"]), default=0) or 1
        rows = []
        for t in a["types"]:
            names = [f["name"] for f in t["fields"]]
            prev = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
            prev = _esc(prev) if names else "<em>no fields</em>"
            barw = max(3, round(t["count"] / maxc * 120)) if t["count"] else 0
            bar = (f"<div class='bar' style='width:{barw}px'></div>"
                   if t["count"] else "")
            rows.append(
                f"<tr class='trow' tabindex='0' data-app=\"{_esc(a['app'])}\" "
                f"data-type=\"{_esc(t['name'])}\">"
                f"<td><div class='tname'>{_esc(t['name'])}</div></td>"
                f"<td><div class='tprev'>{prev}</div></td>"
                f"<td class='tcount'><span class='n'>{t['count']:,}</span>{bar}</td></tr>")
        table = (
            "<div class='panel'><div class='panel__cap'><h3>Types</h3>"
            "<span class='muted' style='font:11px/1 var(--mono)'>click a row for fields</span></div>"
            "<table><thead><tr><th>type</th><th>fields</th>"
            "<th class='r'>objects</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")
        graph = (
            "<div class='panel'><div class='panel__cap'><h3>Data model</h3>"
            "<span class='muted' style='font:11px/1 var(--mono)'>click a node for fields</span></div>"
            f"<div class='graph' data-app=\"{_esc(a['app'])}\"></div></div>")
        body = graph + table
    else:
        body = ("<div class='panel'><div class='empty'>No types yet. Define one by "
                "calling the MorphDB <code>add_field</code> tool.</div></div>")

    return (
        f"<section class='app{open_cls}'>"
        f"<button class='app__head' aria-expanded='{expanded}'>"
        f"<span class='app__chev'>›</span>"
        f"<span class='app__key'>{_esc(a['app'])}</span>"
        f"<span class='app__meta'>{meta}</span></button>"
        f"<div class='app__body'>{body}</div></section>")


def _cell(v):
    """One raw table cell: NULLs marked, long values clipped (full text on hover)."""
    if v is None:
        return "<span class='null'>NULL</span>"
    s = str(v)
    if len(s) > 200:
        return f"<span title=\"{_esc(s)}\">{_esc(s[:200])}…</span>"
    return _esc(s)


def _raw_tables_html(tables):
    """The 'Tables · raw' view: every SQLite table with its columns and rows."""
    if not tables:
        return "<div class='empty'>No tables in this database.</div>"
    out = []
    for t in tables:
        head = "".join(f"<th>{_esc(col)}</th>" for col in t["columns"])
        if t["rows"]:
            body = "".join(
                "<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in row) + "</tr>"
                for row in t["rows"])
        else:
            span = max(1, len(t["columns"]))
            body = f"<tr><td colspan='{span}' class='rawempty'>— empty —</td></tr>"
        capped = t["total"] > t["shown"]
        note = (f"showing first <b>{t['shown']:,}</b> of <b>{t['total']:,}</b> rows"
                if capped else f"<b>{t['total']:,}</b> row{'s' * (t['total'] != 1)}")
        out.append(
            "<div class='panel rawtable'>"
            f"<div class='panel__cap'><h3>{_esc(t['name'])}</h3>"
            f"<span class='rawnote'>{note}</span></div>"
            f"<div class='tscroll'><table class='raw'><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div></div>")
    return "".join(out)


def render(data, db):
    if data.get("error"):
        body = (f"<div class='err'>{_esc(data['error'])}<br><br>"
                f"Point the dashboard at a MorphDB database with "
                f"<code>morphdb dashboard --db PATH</code>.</div>")
    else:
        apps = data["apps"]
        if apps:
            n_obj = sum(t["count"] for a in apps for t in a["types"])
            n_types = sum(len(a["types"]) for a in apps)
            n_rel = sum(len(a["relations"]) for a in apps)
            summary = (
                "<div class='summary'>"
                f"<div class='stat'><div class='num'>{len(apps):,}</div><div class='lab'>Apps</div></div>"
                f"<div class='stat'><div class='num'>{n_types:,}</div><div class='lab'>Types</div></div>"
                f"<div class='stat'><div class='num'>{n_rel:,}</div><div class='lab'>Relations</div></div>"
                f"<div class='stat'><div class='num'>{n_obj:,}</div><div class='lab'>Objects</div></div>"
                "</div>")
            model_view = summary + "".join(_app_card(a, i) for i, a in enumerate(apps))
        else:
            model_view = ("<div class='empty'>No apps yet. Register one with "
                          "<code>POST /app</code> (or the <code>register_app</code> "
                          "tool) and it shows up here.</div>")
        body = (
            "<div class='tabs'>"
            "<button class='tab tab--on' type='button' data-view='model'>Data model</button>"
            "<button class='tab' type='button' data-view='tables'>Tables · raw</button>"
            "</div>"
            f"<div class='view view--on' id='view-model'>{model_view}</div>"
            f"<div class='view' id='view-tables'>{_raw_tables_html(data.get('tables', []))}</div>")

    model = json.dumps(data.get("apps", [])).replace("</", "<\\/")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>MorphDB admin</title><style>" + _CSS + "</style></head><body>"
        "<div class='wrap'><div class='top'>"
        "<div class='brand'><b>MorphDB</b><span class='slash'>/</span>"
        "<span class='sub'>admin</span></div>"
        f"<div class='dbpath'>{_esc(db)}</div></div>"
        + body +
        "<footer>read-only · reading <b>" + _esc(db) + "</b> · refresh to update</footer>"
        "</div>"
        "<div class='modal' id='modal'><div class='sheet' id='sheet'></div></div>"
        "<script>window.__MODEL__=" + model + ";</script>"
        "<script>" + _JS + "</script></body></html>")


_JS = r"""
(function () {
  var MODEL = window.__MODEL__ || [];
  var byApp = {}; MODEL.forEach(function (a) { byApp[a.app] = a; });

  // --- collapse / expand apps ---
  document.querySelectorAll('.app__head').forEach(function (h) {
    h.addEventListener('click', function () {
      var app = h.closest('.app');
      var open = app.classList.toggle('app--open');
      h.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) app.querySelectorAll('.graph[data-app]').forEach(buildGraph);
    });
  });

  // --- cardinality helpers ---
  function ends(card) {            // [fromEndMultiplicity, toEndMultiplicity]
    var p = (card || '').split('_to_');
    return [p[0] === 'many' ? 'many' : 'one', p[1] === 'many' ? 'many' : 'one'];
  }
  function cardShort(card) {
    var e = ends(card);
    return (e[0] === 'many' ? 'N' : '1') + ':' + (e[1] === 'many' ? 'N' : '1');
  }

  var SVGNS = 'http://www.w3.org/2000/svg';
  function el(name, attrs) {
    var n = document.createElementNS(SVGNS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  // point where the segment center->toward exits an axis-aligned box (w x h)
  function boxEdge(cx, cy, w, h, tx, ty) {
    var dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return [cx, cy];
    var s = Math.min(dx ? (w / 2) / Math.abs(dx) : 1e9,
                     dy ? (h / 2) / Math.abs(dy) : 1e9);
    return [cx + dx * s, cy + dy * s];
  }

  // crow's-foot (many) or single bar (one) at point P, dir = unit vector outward
  function footPaths(P, dir, kind) {
    var px = -dir[1], py = dir[0];            // perpendicular
    var out = [];
    if (kind === 'many') {
      var ax = P[0] + dir[0] * 15, ay = P[1] + dir[1] * 15;
      [[P[0] + px * 7, P[1] + py * 7], [P[0], P[1]], [P[0] - px * 7, P[1] - py * 7]]
        .forEach(function (f) { out.push('M' + ax + ',' + ay + 'L' + f[0] + ',' + f[1]); });
    } else {
      var m = [P[0] + dir[0] * 11, P[1] + dir[1] * 11];
      out.push('M' + (m[0] + px * 6) + ',' + (m[1] + py * 6) +
               'L' + (m[0] - px * 6) + ',' + (m[1] - py * 6));
    }
    return out;
  }

  // deterministic force-directed layout (Fruchterman–Reingold). k is the ideal
  // edge length, so connected nodes settle ~k apart and the graph stays compact.
  function layout(nodes, edges, W, H) {
    var n = nodes.length, cx = W / 2, cy = H / 2;
    var k = Math.max(125, 195 - n * 6);
    nodes.forEach(function (nd, i) {
      var ang = (i / n) * Math.PI * 2, R = Math.min(W, H) * 0.3;
      nd.x = cx + Math.cos(ang) * R; nd.y = cy + Math.sin(ang) * R;
    });
    if (n === 1) { nodes[0].x = cx; nodes[0].y = cy; return; }
    var idx = {}; nodes.forEach(function (nd, i) { idx[nd.name] = i; });
    var temp = k * 1.5;
    for (var it = 0; it < 320; it++) {
      var fx = new Array(n).fill(0), fy = new Array(n).fill(0);
      for (var i = 0; i < n; i++) for (var j = i + 1; j < n; j++) {
        var dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        var fr = (k * k) / d, ux = dx / d, uy = dy / d;       // repulsion
        fx[i] += ux * fr; fy[i] += uy * fr; fx[j] -= ux * fr; fy[j] -= uy * fr;
      }
      edges.forEach(function (e) {
        var a = idx[e.from], b = idx[e.to]; if (a === b || a == null || b == null) return;
        var dx = nodes[b].x - nodes[a].x, dy = nodes[b].y - nodes[a].y;
        var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        var fa = (d * d) / k, ux = dx / d, uy = dy / d;        // attraction
        fx[a] += ux * fa; fy[a] += uy * fa; fx[b] -= ux * fa; fy[b] -= uy * fa;
      });
      for (var i = 0; i < n; i++) {
        fx[i] += (cx - nodes[i].x) * 0.06; fy[i] += (cy - nodes[i].y) * 0.06;  // gravity
        var fd = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]) || 0.01;
        var step = Math.min(fd, temp);
        nodes[i].x += fx[i] / fd * step; nodes[i].y += fy[i] / fd * step;
      }
      temp = Math.max(k * 0.05, temp * 0.985);                 // cool down
    }
  }

  function bbox(ns) {
    var minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
    ns.forEach(function (n) {
      minx = Math.min(minx, n.x - n.w / 2); maxx = Math.max(maxx, n.x + n.w / 2);
      miny = Math.min(miny, n.y - n.h / 2); maxy = Math.max(maxy, n.y + n.h / 2);
    });
    return {minx: minx, miny: miny, maxx: maxx, maxy: maxy};
  }

  // pack a list of nodes into a tidy grid with its top-left at (leftX, topY)
  function gridLayout(list, leftX, topY) {
    var nn = list.length; if (!nn) return;
    var cols = Math.max(1, Math.ceil(Math.sqrt(nn)));
    var maxW = 0; list.forEach(function (n) { maxW = Math.max(maxW, n.w); });
    var cw2 = maxW + 30, ch2 = 48 + 30;
    list.forEach(function (n, i) {
      n.x = leftX + (i % cols) * cw2 + cw2 / 2;
      n.y = topY + Math.floor(i / cols) * ch2 + ch2 / 2;
    });
  }

  function buildGraph(container) {
    if (container.dataset.built) return;
    container.dataset.built = '1';
    var app = byApp[container.dataset.app]; if (!app) return;
    var rels = app.relations || [];
    // every type is a node — independent types included; also add any type that
    // exists only as a relation endpoint.
    var typesByName = {}; (app.types || []).forEach(function (t) { typesByName[t.name] = t; });
    var nodeNames = (app.types || []).map(function (t) { return t.name; });
    rels.forEach(function (r) {
      [r.from, r.to].forEach(function (nm) {
        if (nodeNames.indexOf(nm) < 0) { nodeNames.push(nm); typesByName[nm] = {name: nm, fields: [], count: 0}; }
      });
    });
    if (!nodeNames.length) return;

    // lay out in a world space sized to the node count; pan/zoom maps it to view
    var cols = Math.ceil(Math.sqrt(nodeNames.length));
    var Wd = Math.max(640, cols * 210);
    var Hd = Math.max(420, Math.ceil(nodeNames.length / cols) * 165);
    var nodes = nodeNames.map(function (nm) {
      var t = typesByName[nm];
      return {name: nm, w: Math.max(104, nm.length * 8.8 + 34), h: 48,
              fields: (t.fields || []).length, count: t.count || 0};
    });
    // connected nodes get the force layout; isolated nodes are packed in a tidy
    // grid below it — so an app with no/few relations reads cleanly instead of
    // scattering into unreadable specks.
    var connSet = {}; rels.forEach(function (r) { connSet[r.from] = 1; connSet[r.to] = 1; });
    var conn = nodes.filter(function (n) { return connSet[n.name]; });
    var iso = nodes.filter(function (n) { return !connSet[n.name]; });
    if (conn.length) {
      layout(conn, rels, Wd, Hd);
      if (iso.length) { var cb = bbox(conn); gridLayout(iso, cb.minx, cb.maxy + 64); }
    } else {
      gridLayout(nodes, 0, 0);
    }
    var pos = {}; nodes.forEach(function (n) { pos[n.name] = n; });

    var rect = container.getBoundingClientRect();
    var cw = Math.round(rect.width) || 760, ch = Math.round(rect.height) || 560;
    var svg = el('svg', {viewBox: '0 0 ' + cw + ' ' + ch, role: 'img',
                         'aria-label': 'Data model graph for ' + app.app});
    var vp = el('g', {class: 'vp'});

    // edges + crow's feet
    rels.forEach(function (r) {
      var A = pos[r.from], B = pos[r.to]; if (!A || !B) return;
      var em = ends(r.cardinality);
      var g = el('g', {'data-from': r.from, 'data-to': r.to});
      if (r.from === r.to) {                  // self-loop (often symmetric)
        var lx = A.x, ly = A.y - A.h / 2;
        var d = 'M' + (lx - 16) + ',' + ly + ' C' + (lx - 60) + ',' + (ly - 70) + ' ' +
                (lx + 60) + ',' + (ly - 70) + ' ' + (lx + 16) + ',' + ly;
        g.appendChild(el('path', {class: 'gedge', d: d}));
        footPaths([lx - 16, ly], [0, -1], em[0]).concat(footPaths([lx + 16, ly], [0, -1], em[1]))
          .forEach(function (p) { g.appendChild(el('path', {class: 'gfoot', d: p})); });
        addLabel(g, lx, ly - 80, r.forward + (r.inverse && r.inverse !== r.forward ? ' / ' + r.inverse : ''), 'grelname');
        addLabel(g, lx, ly - 68, cardShort(r.cardinality));
      } else {
        var pa = boxEdge(A.x, A.y, A.w, A.h, B.x, B.y);
        var pb = boxEdge(B.x, B.y, B.w, B.h, A.x, A.y);
        var dax = pb[0] - pa[0], day = pb[1] - pa[1];
        var dl = Math.sqrt(dax * dax + day * day) || 1; var u = [dax / dl, day / dl];
        // shrink line a touch so the feet read cleanly
        var la = [pa[0] + u[0] * 16, pa[1] + u[1] * 16], lb = [pb[0] - u[0] * 16, pb[1] - u[1] * 16];
        g.appendChild(el('path', {class: 'gedge', d: 'M' + la[0] + ',' + la[1] + 'L' + lb[0] + ',' + lb[1]}));
        footPaths(pa, u, em[0]).forEach(function (p) { g.appendChild(el('path', {class: 'gfoot', d: p})); });
        footPaths(pb, [-u[0], -u[1]], em[1]).forEach(function (p) { g.appendChild(el('path', {class: 'gfoot', d: p})); });
        var pn = [-u[1], u[0]];                       // perpendicular to the edge
        var fP = lerp(pa, pb, 0.30), iP = lerp(pa, pb, 0.70), mP = lerp(pa, pb, 0.5);
        addLabel(g, fP[0] + pn[0] * 11, fP[1] + pn[1] * 11, r.forward, 'grelname');
        addLabel(g, iP[0] + pn[0] * 11, iP[1] + pn[1] * 11, r.inverse, 'grelname inv');
        addLabel(g, mP[0] - pn[0] * 12, mP[1] - pn[1] * 12, cardShort(r.cardinality));
      }
      vp.appendChild(g);
    });

    function addLabel(g, x, y, text, cls) {
      if (!text) return;
      var t = el('text', {class: cls || 'grellabel', x: x, y: y, 'text-anchor': 'middle'});
      t.textContent = text; g.appendChild(t);
    }
    function lerp(p, q, t) { return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]; }

    // nodes
    nodes.forEach(function (n) {
      var g = el('g', {class: 'gnode', tabindex: '0', 'data-name': n.name,
                       role: 'button', 'aria-label': 'type ' + n.name});
      g.appendChild(el('rect', {x: n.x - n.w / 2, y: n.y - n.h / 2, width: n.w, height: n.h, rx: 9}));
      var t1 = el('text', {class: 'gn-name', x: n.x, y: n.y - 2, 'text-anchor': 'middle'});
      t1.textContent = n.name; g.appendChild(t1);
      var t2 = el('text', {class: 'gn-sub', x: n.x, y: n.y + 13, 'text-anchor': 'middle'});
      t2.textContent = n.fields + 'f · ' + fmt(n.count) + ' obj'; g.appendChild(t2);
      g.addEventListener('click', function () { openModal(app.app, n.name); });
      g.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(app.app, n.name); }
      });
      g.addEventListener('mouseenter', function () { highlight(container, svg, n.name); });
      g.addEventListener('mouseleave', function () { container.classList.remove('dim'); clearLit(svg); });
      vp.appendChild(g);
    });

    svg.appendChild(vp);
    container.appendChild(svg);

    // --- pan & zoom ---
    var view = {k: 1, x: 0, y: 0};
    function apply() { vp.setAttribute('transform', 'translate(' + view.x + ',' + view.y + ') scale(' + view.k + ')'); }
    function clampK(k) { return Math.max(0.15, Math.min(4, k)); }
    function zoomAt(mx, my, f) {
      var nk = clampK(view.k * f);
      view.x = mx - (mx - view.x) * (nk / view.k);
      view.y = my - (my - view.y) * (nk / view.k);
      view.k = nk; apply();
    }
    function fit() {
      var b = bbox(nodes);
      var bw = Math.max(1, b.maxx - b.minx), bh = Math.max(1, b.maxy - b.miny), pad = 60;
      view.k = clampK(Math.min((cw - pad * 2) / bw, (ch - pad * 2) / bh, 2.0));
      view.x = (cw - bw * view.k) / 2 - b.minx * view.k;
      view.y = (ch - bh * view.k) / 2 - b.miny * view.k;
      apply();
    }
    svg.addEventListener('wheel', function (e) {
      e.preventDefault();
      var r = svg.getBoundingClientRect();
      zoomAt((e.clientX - r.left) * (cw / r.width), (e.clientY - r.top) * (ch / r.height),
             Math.exp(-e.deltaY * 0.0016));
    }, {passive: false});
    var drag = null;
    svg.addEventListener('pointerdown', function (e) {
      if (e.target.closest && e.target.closest('.gnode')) return;   // node = click, not pan
      drag = {x: e.clientX, y: e.clientY, ox: view.x, oy: view.y};
      svg.classList.add('panning'); try { svg.setPointerCapture(e.pointerId); } catch (_) {}
    });
    svg.addEventListener('pointermove', function (e) {
      if (!drag) return; var r = svg.getBoundingClientRect();
      view.x = drag.ox + (e.clientX - drag.x) * (cw / r.width);
      view.y = drag.oy + (e.clientY - drag.y) * (ch / r.height); apply();
    });
    function endDrag() { drag = null; svg.classList.remove('panning'); }
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);

    // tool buttons + hint
    var tools = document.createElement('div'); tools.className = 'gtools';
    function tbtn(label, title, fn, cls) {
      var b = document.createElement('button'); b.type = 'button'; b.textContent = label;
      b.title = title; if (cls) b.className = cls; b.onclick = fn; tools.appendChild(b);
    }
    tbtn('−', 'Zoom out', function () { zoomAt(cw / 2, ch / 2, 0.8); });
    tbtn('fit', 'Fit to view', function () { fit(); }, 'fit');
    tbtn('+', 'Zoom in', function () { zoomAt(cw / 2, ch / 2, 1.25); });
    container.appendChild(tools);
    var hint = document.createElement('div'); hint.className = 'ghint';
    hint.textContent = 'scroll to zoom · drag to pan · click a type for fields';
    container.appendChild(hint);
    fit();

    if (rels.length) {              // cardinality key only when there are edges
      var leg = document.createElement('div'); leg.className = 'glegend';
      var lt = document.createElement('span'); lt.className = 'lg-t';
      lt.textContent = 'cardinality'; leg.appendChild(lt);
      [['one_to_one', '1:1'], ['one_to_many', '1:N'], ['many_to_one', 'N:1'], ['many_to_many', 'N:N']]
        .forEach(function (c) {
          var d = document.createElement('div'); d.innerHTML = legendSvg(c[0]) + c[1]; leg.appendChild(d);
        });
      container.after(leg);
    }
  }

  function highlight(container, svg, name) {
    container.classList.add('dim');
    svg.querySelectorAll('.gnode').forEach(function (g) {
      g.classList.toggle('lit', g.getAttribute('data-name') === name);
    });
    svg.querySelectorAll('g[data-from]').forEach(function (g) {
      var on = g.getAttribute('data-from') === name || g.getAttribute('data-to') === name;
      if (on) { g.querySelectorAll('.gedge,.gfoot').forEach(function (p) { p.classList.add('lit'); });
        ['data-from', 'data-to'].forEach(function (a) {
          var nm = g.getAttribute(a);
          svg.querySelector('.gnode[data-name="' + cssq(nm) + '"]')?.classList.add('lit');
        });
      }
    });
  }
  function clearLit(svg) {
    svg.querySelectorAll('.lit').forEach(function (e) { e.classList.remove('lit'); });
  }
  function cssq(s) { return (s || '').replace(/"/g, '\\"'); }

  function legendSvg(card) {
    var e = ends(card);
    var s = '<svg width="42" height="16" viewBox="0 0 42 16" style="vertical-align:middle">';
    s += '<line x1="13" y1="8" x2="29" y2="8" stroke="var(--accent-dim)" stroke-width="1.4"/>';
    s += foot(13, e[0] === 'many' ? -1 : 0, e[0]);
    s += foot(29, e[1] === 'many' ? 1 : 0, e[1]);
    return s + '</svg>';
    function foot(x, dir, kind) {
      if (kind === 'many') {
        var ax = x + dir * 9;
        return '<path d="M' + ax + ',8 L' + x + ',3 M' + ax + ',8 L' + x + ',8 M' + ax + ',8 L' + x + ',13" stroke="var(--accent)" stroke-width="1.4" fill="none"/>';
      }
      return '<line x1="' + x + '" y1="3" x2="' + x + '" y2="13" stroke="var(--accent)" stroke-width="1.4"/>';
    }
  }

  function nmeName(s) { return s.replace(/[&<>]/g, function (c) { return ({'&': '&amp;', '<': '&lt;', '>': '&gt;'})[c]; }); }
  function fmt(n) { return (n || 0).toLocaleString(); }

  // --- modal ---
  var modal = document.getElementById('modal'), sheet = document.getElementById('sheet');
  function openModal(appName, typeName) {
    var app = byApp[appName]; if (!app) return;
    var t = (app.types || []).find(function (x) { return x.name === typeName; }) || {name: typeName, fields: [], count: 0};
    var fields = (t.fields || []).map(function (f) {
      return '<div class="frow"><span class="fn">' + nmeName(f.name) + '</span>' +
             '<span class="ft">' + nmeName(f.type) + '</span></div>';
    }).join('') || '<div class="none">No fields.</div>';

    var rels = (app.relations || []).filter(function (r) { return r.from === typeName || r.to === typeName; })
      .map(function (r) {
        var fwd = r.from === typeName;
        var fieldName = fwd ? r.forward : r.inverse;
        var other = fwd ? r.to : r.from;
        return '<div class="rel"><span class="rn">' + nmeName(fieldName) + '</span> ' +
               '<span class="arrow">→</span> ' + nmeName(other) +
               '  <span class="rc">' + cardShort(r.cardinality) + '</span>' +
               (r.symmetric ? ' <small>symmetric</small>' : '') + '</div>';
      }).join('') || '<div class="none">No relations.</div>';

    sheet.innerHTML =
      '<div class="sheet__top"><div class="sheet__title">' + nmeName(typeName) + '</div>' +
      '<div class="sheet__obj"><b>' + fmt(t.count) + '</b>objects</div>' +
      '<button class="x" aria-label="Close">×</button></div>' +
      '<section><h4>Fields</h4>' + fields + '</section>' +
      '<section><h4>Relations</h4>' + rels + '</section>';
    sheet.querySelector('.x').onclick = closeModal;
    modal.classList.add('show');
    sheet.querySelector('.x').focus();
  }
  function closeModal() { modal.classList.remove('show'); }
  modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

  // table rows open the same modal
  document.querySelectorAll('.trow').forEach(function (row) {
    function go() { openModal(row.dataset.app, row.dataset.type); }
    row.addEventListener('click', go);
    row.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
  });

  // --- tabs: Data model <-> Tables (raw) ---
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var v = btn.dataset.view;
      document.querySelectorAll('.tab').forEach(function (b) {
        b.classList.toggle('tab--on', b === btn);
      });
      document.querySelectorAll('.view').forEach(function (view) {
        view.classList.toggle('view--on', view.id === 'view-' + v);
      });
      // graphs measure their container, so (re)build any now-visible open ones
      if (v === 'model') {
        document.querySelectorAll('.app--open .graph[data-app]').forEach(buildGraph);
      }
    });
  });

  // build graphs already open on load
  document.querySelectorAll('.app--open .graph[data-app]').forEach(buildGraph);
})();
"""


def serve(target, host="127.0.0.1", port=8788, open_browser=True):
    try:
        display = _backend.from_target(target).describe()   # masks any credentials
    except Exception:
        display = str(target)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = render(gather(target), display).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"MorphDB admin dashboard: {url}\n  reading: {display}\n  Ctrl-C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
