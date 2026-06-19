"""Round-2 regression tests: bugs found by the ultracode workflow.

Covers json NaN poison, datetime normalization, projection-aware queries,
symmetric-flip canonicalization, GET-body keep-alive desync, and the smaller
validation gaps. Same harness style as the other test modules.
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

    def total(self, p):
        st, b, _ = self.get(p)
        self.assertEqual(st, 200, b)
        return b["total"]


class TestJsonNonFinite(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "t", "fields": {"j": "json"}})

    def test_json_nan_literal_rejected(self):
        # Python's json.loads accepts bare NaN; the write must still reject it.
        st, _, _ = req("POST", "/objects/t", raw_body=b'{"j": NaN}')
        self.assertEqual(st, 400)

    def test_json_nested_infinity_rejected(self):
        st, _, _ = req("POST", "/objects/t", raw_body=b'{"j": {"a": [1, Infinity, 3]}}')
        self.assertEqual(st, 400)

    def test_json_default_nan_rejected_at_schema_time(self):
        st, _, _ = req("POST", "/schemas/objects",
                       raw_body=b'{"name":"tp","fields":{"j":{"type":"json","default":NaN}}}')
        self.assertEqual(st, 400)
        # global introspection must still work
        st, _, _ = self.get("/schema")
        self.assertEqual(st, 200)

    def test_type_not_bricked_after_attempt(self):
        req("POST", "/objects/t", raw_body=b'{"j": NaN}')
        st, b, _ = self.get("/objects/t")
        self.assertEqual(st, 200)
        self.assertEqual(b["total"], 0)


class TestDatetimeNormalized(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "ev", "fields": {"at": "datetime"}})

    def test_equivalent_forms_compare_equal(self):
        for v in ("2020-01-01T12:00:00Z", "2020-01-01T12:00:00+00:00",
                  "2020-01-01T12:00:00"):
            self.post("/objects/ev", {"at": v})
        # all three are the same instant
        self.assertEqual(self.total("/objects/ev?at=2020-01-01T12:00:00Z"), 3)
        self.assertEqual(self.total("/objects/ev?at__gte=2020-01-01T12:00:00Z"), 3)

    def test_offset_range_filter_correct(self):
        # 08:00+05:00 == 03:00Z, which is before the 10:00Z threshold
        self.post("/objects/ev", {"at": "2020-01-01T08:00:00+05:00"})
        self.assertEqual(self.total("/objects/ev?at__lt=2020-01-01T10:00:00Z"), 1)
        self.assertEqual(self.total("/objects/ev?at__gt=2020-01-01T10:00:00Z"), 0)

    def test_sort_is_chronological(self):
        self.post("/objects/ev", {"at": "2020-01-01 20:00:00"})   # space form, later
        self.post("/objects/ev", {"at": "2020-01-01T08:00:00"})   # T form, earlier
        st, b, _ = self.get("/objects/ev?sort=at&order=asc")
        ats = [o["at"] for o in b["objects"]]
        self.assertEqual(ats, sorted(ats))               # canonical => lexical == chrono
        self.assertTrue(ats[0].startswith("2020-01-01T08"))

    def test_epoch_write_and_query(self):
        self.post("/objects/ev", {"at": 1577880000})     # epoch seconds
        # queryable by the same epoch (string in the query)
        self.assertEqual(self.total("/objects/ev?at=1577880000"), 1)


class TestProjectionAwareQueries(Base):
    def test_default_added_by_merge_is_queryable(self):
        self.post("/schemas/objects", {"name": "d", "fields": {"n": "number"}})
        st, b, _ = self.post("/objects/d", {"n": 1})     # created before status exists
        # add a defaulted field to a type that already has data
        req("PUT", "/schemas/objects/d",
            {"merge": True, "fields": {"status": {"type": "string", "default": "open"}}})
        # read shows the default, AND query/exists agree
        st, b2, _ = self.get(f"/objects/d/{b['_guid']}")
        self.assertEqual(b2["status"], "open")
        self.assertEqual(self.total("/objects/d?status=open"), 1)
        self.assertEqual(self.total("/objects/d?status__exists=true"), 1)

    def test_retype_uncoercible_not_false_matched(self):
        self.post("/schemas/objects", {"name": "r", "fields": {"v": "string"}})
        self.post("/objects/r", {"v": "hello"})
        req("PUT", "/schemas/objects/r", {"merge": True, "fields": {"v": "number"}})
        # read projects null; a numeric range filter must NOT match it
        self.assertEqual(self.total("/objects/r?v__gt=0"), 0)

    def test_sort_uses_projected_defaults(self):
        self.post("/schemas/objects", {"name": "s", "fields": {"rank": "number"}})
        self.post("/objects/s", {})                       # no rank yet
        req("PUT", "/schemas/objects/s",
            {"merge": True, "fields": {"rank": {"type": "number", "default": 5}}})
        self.post("/objects/s", {"rank": 1})
        st, b, _ = self.get("/objects/s?sort=rank&order=asc")
        ranks = [o["rank"] for o in b["objects"]]
        self.assertEqual(ranks, [1, 5])                   # default 5 ordered as 5


class TestSymmetricFlip(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.a = self.post("/objects/u", {"n": "a"})[1]["_guid"]
        self.b = self.post("/objects/u", {"n": "b"})[1]["_guid"]

    def test_flip_canonicalizes_existing_edges(self):
        # non-symmetric m2m self relation with both directions present
        self.post("/schemas/associations", {
            "name": "buddy", "from_type": "u", "to_type": "u",
            "forward_name": "buddies", "inverse_name": "buddies",
            "cardinality": "many_to_many"})
        self.post("/associations", {"assoc_name": "buddy", "from_guid": self.a, "to_guid": self.b})
        self.post("/associations", {"assoc_name": "buddy", "from_guid": self.b, "to_guid": self.a})
        self.assertEqual(self.total(f"/object/{self.a}/associations"), 2)  # dup pre-flip
        # flip to symmetric -> canonicalize + dedup
        req("PUT", "/schemas/associations/buddy", {
            "from_type": "u", "to_type": "u", "forward_name": "buddies",
            "cardinality": "many_to_many", "symmetric": True})
        self.assertEqual(self.total(f"/object/{self.a}/associations"), 1)
        self.assertEqual(self.total(f"/object/{self.b}/associations"), 1)

    def test_symmetric_inverse_name_forced(self):
        st, b, _ = self.post("/schemas/associations", {
            "name": "friend", "from_type": "u", "to_type": "u",
            "forward_name": "friends", "inverse_name": "DIFFERENT",
            "cardinality": "many_to_many", "symmetric": True})
        self.assertEqual(b["inverse_name"], "friends")   # forced to forward_name


class TestValidationGaps(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.guid = self.post("/objects/u", {"n": "x"})[1]["_guid"]

    def test_field_name_trailing_newline_rejected(self):
        st, _, _ = req("POST", "/schemas/objects",
                       raw_body=b'{"name":"bad","fields":{"city\\n":"string"}}')
        self.assertEqual(st, 400)

    def test_invalid_direction_rejected(self):
        st, _, _ = self.get(f"/object/{self.guid}/associations?direction=sideways")
        self.assertEqual(st, 400)


class TestGetBodyKeepAlive(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")

    def test_get_with_body_does_not_desync(self):
        s = socket.create_connection(("127.0.0.1", _PORT), timeout=5)
        body = b'{"junk":"AAAA"}'
        first = (
            "GET /help HTTP/1.1\r\nHost: x\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        second = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(first)
        s.sendall(second)
        s.settimeout(5)
        data = b""
        try:
            while b'"status": "ok"' not in data and b"501" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        self.assertNotIn(b"501", data)
        self.assertNotIn(b"Unsupported method", data)
        self.assertGreaterEqual(data.count(b"HTTP/1.1 200"), 2)
        self.assertIn(b'"status": "ok"', data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
