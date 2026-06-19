"""Round-5 regression tests: the two HIGH bugs from ultracode battle-test 4.

1. Deep-nested JSON accepted on write then 500s on read (recursion).
2. Drop + re-add of a field at a different type bypassed retype migration.
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


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_PORT = _free_port()
_BASE = f"http://127.0.0.1:{_PORT}"
_HTTPD = None


def setUpModule():
    global _HTTPD
    os.environ["MORPHDB_QUIET"] = "1"
    db.init_db(":memory:")
    _HTTPD = MorphServer(("127.0.0.1", _PORT), Handler)
    threading.Thread(target=_HTTPD.serve_forever, daemon=True).start()


def tearDownModule():
    if _HTTPD is not None:
        _HTTPD.shutdown()
        _HTTPD.server_close()


def req(method, path, body=None, raw_body=None):
    url = _BASE + path
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None), resp
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e


class Base(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")

    def post(self, p, b=None):
        return req("POST", p, b)

    def get(self, p):
        return req("GET", p)


class TestDeepJson(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "j", "fields": {"v": "json"}})

    def test_deeply_nested_rejected(self):
        payload = b'{"v":' + b'{"a":' * 982 + b'1' + b'}' * 982 + b'}'
        st, _, _ = req("POST", "/objects/j", raw_body=payload)
        self.assertEqual(st, 400)
        # and the type's list endpoint is unharmed
        st, b, _ = self.get("/objects/j")
        self.assertEqual(st, 200)
        self.assertEqual(b["total"], 0)

    def test_reasonable_nesting_ok_and_readable(self):
        val = cur = {}
        for _ in range(20):
            cur["a"] = {}
            cur = cur["a"]
        cur["leaf"] = 1
        st, b, _ = self.post("/objects/j", {"v": val})
        self.assertEqual(st, 201)
        # reads back fine (no recursion 500)
        st, b2, _ = self.get(f"/objects/j/{b['_guid']}")
        self.assertEqual(st, 200)


class TestDropReadd(Base):
    def test_readd_at_different_type_reads_unset(self):
        # number 5 left in the blob does not satisfy a boolean field -> unset
        self.post("/schemas/objects", {"name": "item", "fields": {"qty": "number"}})
        g = self.post("/objects/item", {"qty": 5})[1]["_guid"]
        self.post("/schemas/objects/item/delete-fields", {"fields": ["qty"]})
        req("PUT", "/schemas/objects/item",
            {"merge": True, "fields": {"qty": "boolean"}})
        st, b, _ = self.get(f"/objects/item/{g}")
        self.assertIsNone(b["qty"])

    def test_readd_same_type_recovers_value(self):
        # the SKILL promise: dropping then re-adding the SAME type recovers data
        # (the value still matches the type, so lazy projection returns it)
        self.post("/schemas/objects", {"name": "item", "fields": {"qty": "number"}})
        g = self.post("/objects/item", {"qty": 7})[1]["_guid"]
        self.post("/schemas/objects/item/delete-fields", {"fields": ["qty"]})
        req("PUT", "/schemas/objects/item",
            {"merge": True, "fields": {"qty": "number"}})
        st, b, _ = self.get(f"/objects/item/{g}")
        self.assertEqual(b["qty"], 7)

    def test_readd_string_value_at_number_reads_unset(self):
        # a string "42" left over does not satisfy a number field -> unset,
        # and the eq-query agrees (no cross-type coercion in lazy mode)
        self.post("/schemas/objects", {"name": "p", "fields": {"v": "string"}})
        g = self.post("/objects/p", {"v": "42"})[1]["_guid"]
        self.post("/schemas/objects/p/delete-fields", {"fields": ["v"]})
        req("PUT", "/schemas/objects/p", {"merge": True, "fields": {"v": "number"}})
        st, b, _ = self.get(f"/objects/p/{g}")
        self.assertIsNone(b["v"])
        st, q, _ = self.get("/objects/p?v=42")
        self.assertEqual(q["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
