"""Read-only admin dashboard: every app and its tables, in one local page.

Operator-facing and local-only — it opens the SQLite file directly (read-only)
rather than going through the HTTP API, so it can list apps without adding a
"list apps" endpoint to the public surface (which is intentionally absent).
"""

import html
import json
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def gather(db):
    """A read-only snapshot: {apps: [{app, types:[{name,fields,count}], relations, edges}]}.

    Tolerates a missing/empty/locked database by returning an ``error`` string.
    """
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        c.row_factory = sqlite3.Row
    except Exception as e:
        return {"error": f"cannot open database: {e}", "apps": []}
    try:
        try:
            apps = [r["key"] for r in c.execute("SELECT key FROM apps ORDER BY key")]
        except sqlite3.OperationalError:
            return {"error": "no MorphDB schema in this database yet", "apps": []}
        out = []
        for app in apps:
            types = []
            for r in c.execute(
                    "SELECT name, fields FROM object_schemas WHERE app=? ORDER BY name",
                    (app,)):
                try:
                    fields = list(json.loads(r["fields"]).keys())
                except Exception:
                    fields = []
                count = c.execute(
                    "SELECT COUNT(*) FROM objects WHERE app=? AND object_type=?",
                    (app, r["name"])).fetchone()[0]
                types.append({"name": r["name"], "fields": fields, "count": count})
            relations = c.execute(
                "SELECT COUNT(*) FROM association_schemas WHERE app=?", (app,)).fetchone()[0]
            edges = c.execute(
                "SELECT COUNT(*) FROM associations WHERE app=?", (app,)).fetchone()[0]
            out.append({"app": app, "types": types,
                        "relations": relations, "edges": edges})
        return {"apps": out}
    finally:
        c.close()


_CSS = """
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 900px; margin: 32px auto; padding: 0 16px; }
  header { display:flex; align-items:baseline; justify-content:space-between; gap:12px;
           border-bottom:1px solid #8884; padding-bottom:12px; margin-bottom:20px; }
  h1 { font-size:1.3rem; margin:0; }
  .db { color:#888; font-size:12px; font-family:ui-monospace,Menlo,monospace; }
  .card { border:1px solid #8884; border-radius:10px; padding:14px 16px; margin-bottom:16px; }
  .card h2 { font-size:1.05rem; margin:0 0 2px; }
  .meta { color:#888; font-size:12px; margin-bottom:10px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #8882; }
  th { color:#888; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  td.n { text-align:right; font-variant-numeric:tabular-nums; }
  .muted { color:#999; } .err { color:#ef4444; }
  code { font-family:ui-monospace,Menlo,monospace; background:#8881; padding:1px 5px; border-radius:5px; }
  footer { color:#999; font-size:12px; margin-top:24px; }
"""


def render(data, db):
    esc = html.escape
    if data.get("error"):
        body = f"<p class='err'>{esc(data['error'])}</p>"
    elif not data["apps"]:
        body = "<p class='muted'>No apps registered yet. Create one with <code>POST /app</code>.</p>"
    else:
        cards = []
        for a in data["apps"]:
            if a["types"]:
                trows = "".join(
                    f"<tr><td><b>{esc(t['name'])}</b></td>"
                    f"<td>{esc(', '.join(t['fields']) or '—')}</td>"
                    f"<td class='n'>{t['count']}</td></tr>"
                    for t in a["types"])
            else:
                trows = "<tr><td colspan='3' class='muted'>no types yet</td></tr>"
            cards.append(
                f"<section class='card'><h2>{esc(a['app'])}</h2>"
                f"<div class='meta'>{len(a['types'])} types · "
                f"{a['relations']} relations · {a['edges']} edges</div>"
                "<table><thead><tr><th>type</th><th>fields</th><th>objects</th></tr></thead>"
                f"<tbody>{trows}</tbody></table></section>")
        body = "\n".join(cards)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>MorphDB admin</title><style>{_CSS}</style></head><body>"
        f"<header><h1>MorphDB admin</h1><div class='db'>{esc(str(db))}</div></header>"
        f"{body}<footer>read-only view · refresh to update</footer></body></html>")


def serve(db, host="127.0.0.1", port=8788, open_browser=True):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = render(gather(db), db).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"MorphDB admin dashboard: {url}\n  reading: {db}\n  Ctrl-C to stop.")
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
