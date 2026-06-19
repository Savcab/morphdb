"""Regression tests for bugs found during battle-testing.

Each test pins a specific fix so the bug cannot silently return. Shares the
server fixture style of test_morphdb (own server, in-memory DB reset per test).
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


class TestNumberSanity(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "m", "fields": {"v": "number"}})

    def test_nan_string_rejected(self):
        st, b, _ = self.post("/objects/m", {"v": "NaN"})
        self.assertEqual(st, 400, b)

    def test_infinity_rejected(self):
        for val in ("Infinity", "-inf", "inf"):
            st, _, _ = self.post("/objects/m", {"v": val})
            self.assertEqual(st, 400, val)

    def test_finite_ok(self):
        st, b, _ = self.post("/objects/m", {"v": "3.5"})
        self.assertEqual(st, 201)
        self.assertEqual(b["v"], 3.5)


class TestDatetime(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "ev", "fields": {"at": "datetime"}})

    def test_valid_iso_ok(self):
        for v in ("2026-06-19", "2026-06-19T06:09:12", "2026-06-19T06:09:12.5Z",
                  "2026-06-19T06:09:12+00:00"):
            st, _, _ = self.post("/objects/ev", {"at": v})
            self.assertEqual(st, 201, v)

    def test_garbage_rejected(self):
        for v in ("not-a-date", "2026-13-45T99:99:99Z", "true"):
            st, _, _ = self.post("/objects/ev", {"at": v})
            self.assertEqual(st, 400, v)

    def test_epoch_overflow_rejected(self):
        st, _, _ = self.post("/objects/ev", {"at": 1e20})
        self.assertEqual(st, 400)
        st, _, _ = self.post("/objects/ev", {"at": -99999999999999})
        self.assertEqual(st, 400)


class TestBodyHandling(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "n", "fields": {"t": "string"}})

    def test_non_object_body_rejected(self):
        for raw in (b"[1,2,3]", b"42", b'"hi"'):
            st, _, _ = req("POST", "/objects/n", raw_body=raw)
            self.assertEqual(st, 400, raw)

    def test_deeply_nested_json_is_400_not_500(self):
        raw = (b'{"t":' + b"[" * 5000 + b"1" + b"]" * 5000 + b"}")
        st, _, _ = req("POST", "/objects/n", raw_body=raw)
        self.assertIn(st, (400,), f"expected 400, got {st}")

    def test_invalid_json_400(self):
        st, _, _ = req("POST", "/objects/n", raw_body=b"{not json")
        self.assertEqual(st, 400)

    def test_server_still_healthy_after_bad_input(self):
        req("POST", "/objects/n", raw_body=b"[1,2,3]")
        st, b, _ = self.get("/health")
        self.assertEqual(st, 200)


class TestContainsEscaping(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "n", "fields": {"s": "string"}})
        for s in ("abc", "a%b", "a_b", "axb", "100%done"):
            self.post("/objects/n", {"s": s})

    def test_percent_is_literal(self):
        st, b, _ = self.get("/objects/n?s__contains=%25")  # literal '%'
        titles = sorted(o["s"] for o in b["objects"])
        self.assertEqual(titles, ["100%done", "a%b"])

    def test_underscore_is_literal(self):
        st, b, _ = self.get("/objects/n?s__contains=a_b")
        self.assertEqual([o["s"] for o in b["objects"]], ["a_b"])  # not 'axb'


class TestPutRequiresRequired(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {
            "name": "r", "fields": {"title": {"type": "string", "required": True}}})

    def test_put_missing_required_rejected(self):
        st, b, _ = self.post("/objects/r", {"title": "x"})
        guid = b["_guid"]
        st, _, _ = req("PUT", f"/objects/r/{guid}", {})
        self.assertEqual(st, 400)


class TestDefaultsQueryable(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {
            "name": "d",
            "fields": {"status": {"type": "string", "default": "open"},
                       "n": "number"}})

    def test_default_is_stored_and_queryable(self):
        st, b, _ = self.post("/objects/d", {"n": 1})  # status omitted
        self.assertEqual(b["status"], "open")
        # the default must be visible to a query, not just on read
        st, q, _ = self.get("/objects/d?status=open")
        self.assertEqual(q["total"], 1)
        st, q, _ = self.get("/objects/d?status__exists=false")
        self.assertEqual(q["total"], 0)


class TestRetypeReadView(Base):
    def test_retype_does_not_rewrite_stored_value(self):
        # Stored values are returned as-is; retyping changes validation for
        # future writes only and does not reinterpret existing data. (This keeps
        # reads and queries in lockstep — see the round-2 divergence findings.)
        self.post("/schemas/objects", {"name": "t", "fields": {"v": "number"}})
        st, b, _ = self.post("/objects/t", {"v": 42})
        guid = b["_guid"]
        req("PUT", "/schemas/objects/t", {"merge": True, "fields": {"v": "string"}})
        st, b2, _ = self.get(f"/objects/t/{guid}")
        self.assertEqual(b2["v"], 42)  # returned exactly as stored


class TestLimitOffset(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "n", "fields": {"t": "string"}})
        for i in range(3):
            self.post("/objects/n", {"t": str(i)})

    def test_negative_limit_rejected(self):
        st, _, _ = self.get("/objects/n?limit=-5")
        self.assertEqual(st, 400)

    def test_negative_offset_rejected(self):
        st, _, _ = self.get("/objects/n?offset=-1")
        self.assertEqual(st, 400)


class TestHead(Base):
    def test_head_on_get_route_ok(self):
        st, _, resp = req("HEAD", "/health")
        self.assertEqual(st, 200)


class TestAssocFilterValidation(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.post("/schemas/objects", {"name": "t", "fields": {"x": "string"}})
        self.post("/schemas/associations", {
            "name": "owns", "from_type": "u", "to_type": "t",
            "forward_name": "tasks", "inverse_name": "owner",
            "cardinality": "one_to_many"})
        st, b, _ = self.post("/objects/u", {"n": "a"})
        self.guid = b["_guid"]

    def test_unknown_relation_rejected(self):
        st, _, _ = self.get(f"/object/{self.guid}/associations?relation=bogus")
        self.assertEqual(st, 400)

    def test_unknown_name_rejected(self):
        st, _, _ = self.get(f"/object/{self.guid}/associations?name=bogus")
        self.assertEqual(st, 404)

    def test_known_relation_ok(self):
        st, b, _ = self.get(f"/object/{self.guid}/associations?relation=tasks")
        self.assertEqual(st, 200)
        self.assertEqual(b["total"], 0)


class TestSymmetricAssociations(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.users = {}
        for name in ("a", "b", "c"):
            st, body, _ = self.post("/objects/u", {"n": name})
            self.users[name] = body["_guid"]

    def mk(self, card):
        return self.post("/schemas/associations", {
            "name": "friend", "from_type": "u", "to_type": "u",
            "forward_name": "friends", "cardinality": card, "symmetric": True})

    def test_symmetric_requires_same_type(self):
        self.post("/schemas/objects", {"name": "t", "fields": {"x": "string"}})
        st, _, _ = self.post("/schemas/associations", {
            "name": "bad", "from_type": "u", "to_type": "t",
            "forward_name": "f", "cardinality": "many_to_many", "symmetric": True})
        self.assertEqual(st, 400)

    def test_reverse_edge_deduped(self):
        self.mk("many_to_many")
        a, b = self.users["a"], self.users["b"]
        self.post("/associations", {"assoc_name": "friend", "from_guid": a, "to_guid": b})
        self.post("/associations", {"assoc_name": "friend", "from_guid": b, "to_guid": a})
        st, r, _ = self.get(f"/object/{a}/associations?relation=friends")
        self.assertEqual(r["total"], 1)  # not double-counted
        self.assertEqual(r["associations"][0]["neighbor_guid"], b)
        self.assertEqual(r["associations"][0]["direction"], "symmetric")
        # visible from b's side too
        st, r2, _ = self.get(f"/object/{b}/associations?relation=friends")
        self.assertEqual(r2["total"], 1)
        self.assertEqual(r2["associations"][0]["neighbor_guid"], a)

    def test_symmetric_one_to_one_conflict_both_roles(self):
        self.mk("one_to_one")
        a, b, c = self.users["a"], self.users["b"], self.users["c"]
        st, _, _ = self.post("/associations",
                             {"assoc_name": "friend", "from_guid": a, "to_guid": b})
        self.assertEqual(st, 201)
        # b already has a partner — even though b would be the *from* here
        st, _, _ = self.post("/associations",
                             {"assoc_name": "friend", "from_guid": b, "to_guid": c})
        self.assertEqual(st, 409)

    def test_symmetric_delete_either_order(self):
        self.mk("many_to_many")
        a, b = self.users["a"], self.users["b"]
        self.post("/associations", {"assoc_name": "friend", "from_guid": a, "to_guid": b})
        # delete using the reversed order
        st, _, _ = req("DELETE", "/associations",
                       {"assoc_name": "friend", "from_guid": b, "to_guid": a})
        self.assertEqual(st, 200)
        st, r, _ = self.get(f"/object/{a}/associations")
        self.assertEqual(r["total"], 0)


class TestKeepAliveDesync(unittest.TestCase):
    """The 413 / bad-Content-Length path must not desync a keep-alive socket."""

    def setUp(self):
        db.init_db(":memory:")

    def test_oversize_closes_connection(self):
        s = socket.create_connection(("127.0.0.1", _PORT), timeout=5)
        # Declare a body larger than MAX_BODY but send only a little.
        head = (
            "POST /objects/x HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 99999999\r\n"
            "\r\n"
        ).encode()
        s.sendall(head + b'{"a":1}')
        s.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        self.assertIn(b"413", data.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
