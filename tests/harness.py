"""Shared test harness: one in-process MorphDB server, reset per test.

Zero dependencies — stdlib unittest + urllib only. The server runs in a daemon
thread against an in-memory SQLite db that ``Base.setUp`` re-initializes before
each test, so tests are isolated and order-independent (run serially).

Multi-tenancy: every request needs an ``X-App-Key`` header. ``req`` attaches a
default test app (``APP``) unless ``app`` is overridden (pass ``app=None`` to
omit it, e.g. to test the missing-header path, or another key for isolation).
``Base.setUp`` registers ``APP`` after each db reset.
"""

import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morphdb import db                            # noqa: E402
from morphdb.server import Handler, MorphServer   # noqa: E402

_HTTPD = None
PORT = None
BASE = None
APP = "testapp"          # default app key attached to requests


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ensure_server():
    global _HTTPD, PORT, BASE
    if _HTTPD is not None:
        return
    os.environ["MORPHDB_QUIET"] = "1"
    PORT = _free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    db.init_db(":memory:")
    _HTTPD = MorphServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=_HTTPD.serve_forever, daemon=True).start()


def register_app(key):
    """Register an app key directly (no X-App-Key needed for POST /app)."""
    return req("POST", "/app", {"key": key}, app=None)


def req(method, path, body=None, raw_body=None, app=APP):
    """Return (status, parsed_json_or_none, response). Never raises on HTTP error.

    ``app`` is sent as the X-App-Key header; pass ``app=None`` to omit it.
    """
    ensure_server()
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if app is not None:
        r.add_header("X-App-Key", app)
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None), resp
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e


class Base(unittest.TestCase):
    def setUp(self):
        ensure_server()
        db.init_db(":memory:")
        st, b, _ = register_app(APP)
        self.assertEqual(st, 201, b)

    # convenience verbs ---------------------------------------------------------
    def post(self, p, b=None):
        return req("POST", p, b)

    def get(self, p):
        return req("GET", p)

    def put(self, p, b=None):
        return req("PUT", p, b)

    def patch(self, p, b=None):
        return req("PATCH", p, b)

    def delete(self, p, b=None):
        return req("DELETE", p, b)

    # higher-level helpers ------------------------------------------------------
    def put_type(self, name, **doc):
        st, b, _ = self.put(f"/schema/{name}", doc)
        self.assertEqual(st, 200, b)
        return b

    def create(self, type_name, body):
        st, b, _ = self.post(f"/objects/{type_name}", body)
        self.assertEqual(st, 201, b)
        return b

    def read(self, type_name, guid):
        st, b, _ = self.get(f"/objects/{type_name}/{guid}")
        self.assertEqual(st, 200, b)
        return b

    def total(self, p):
        st, b, _ = self.get(p)
        self.assertEqual(st, 200, b)
        return b["total"]
