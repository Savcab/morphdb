"""Token-gated MorphDB admin dashboard on a Lambda Function URL.

The admin dashboard renders EVERY app's data with no per-user auth, so it must
never be exposed publicly. This handler serves the live, read-only dashboard for
the hosted database, but ONLY when the request carries the secret token
(``?t=<DASH_TOKEN>``); every other request gets a 403. Keep the link private;
rotate by redeploying with a new DASH_TOKEN.

Reuses the existing renderer (morphdb.cli.dashboard.gather + render). stdlib +
psycopg only (psycopg is packaged for Postgres, like the morphdb-api Lambda).
"""

import os
import urllib.parse

from morphdb.cli import dashboard

TOKEN = os.environ.get("DASH_TOKEN", "")
TARGET = os.environ.get("MORPHDB_DATABASE_URL", "")

_DENY = ("<!doctype html><meta charset=utf-8>"
         "<body style='font-family:system-ui;max-width:34rem;margin:12vh auto;padding:0 24px;color:#1a1a1a'>"
         "<h2>403 — private dashboard</h2>"
         "<p>This MorphDB admin dashboard is token-gated. Append "
         "<code>?t=YOUR_TOKEN</code> to the URL to view it.</p></body>")


def _token(event):
    qs = event.get("queryStringParameters")
    if qs and qs.get("t"):
        return qs["t"]
    return urllib.parse.parse_qs(event.get("rawQueryString", "")).get("t", [""])[0]


def handler(event, context):
    if not TOKEN or _token(event) != TOKEN:
        return {"statusCode": 403, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": _DENY}
    try:
        data = dashboard.gather(TARGET)
        body = dashboard.render(data, "hosted Postgres · Neon")
    except Exception as e:  # noqa: BLE001
        return {"statusCode": 500, "headers": {"Content-Type": "text/html; charset=utf-8"},
                "body": "<!doctype html><meta charset=utf-8><pre style='padding:24px'>dashboard error: "
                        + urllib.parse.quote(str(e)) + "</pre>"}
    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": body}
