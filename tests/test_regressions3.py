"""Round-3 regression tests: bugs found by the second ultracode workflow.

Large-integer handling, datetime ambiguity/precision, chunked-body desync,
symmetric direction filtering, and assorted validation gaps.
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


class TestLargeIntegers(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "n", "fields": {"v": "number"}})

    def test_numeric_string_int_exact(self):
        st, b, _ = self.post("/objects/n", {"v": "9223372036854775807"})
        self.assertEqual(st, 201)
        self.assertEqual(b["v"], 9223372036854775807)  # not float-rounded to 2**63

    def test_json_number_int_exact(self):
        st, b, _ = self.post("/objects/n", {"v": 9223372036854775807})
        self.assertEqual(b["v"], 9223372036854775807)

    def test_query_out_of_range_int_no_500(self):
        self.post("/objects/n", {"v": 12345678901234567890})
        for q in ("/objects/n?v=12345678901234567890",
                  "/objects/n?v__gt=99999999999999999999",
                  "/objects/n?v__in=1,99999999999999999999"):
            st, _, _ = self.get(q)
            self.assertEqual(st, 200, q)

    def test_huge_int_default_sort_no_500(self):
        self.post("/schemas/objects", {
            "name": "h",
            "fields": {"v": {"type": "number", "default": 99999999999999999999}}})
        self.post("/objects/h", {})
        st, _, _ = self.get("/objects/h?sort=v")
        self.assertEqual(st, 200)

    def test_underscore_numeric_string_rejected(self):
        st, _, _ = self.post("/objects/n", {"v": "1_000"})
        self.assertEqual(st, 400)


class TestDatetimeEdges(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "e", "fields": {"at": "datetime"}})

    def test_bare_year_rejected_not_epoch(self):
        st, _, _ = self.post("/objects/e", {"at": "2024"})
        self.assertEqual(st, 400)  # not silently parsed as a 1970 instant

    def test_real_epoch_string_accepted_and_queryable(self):
        st, b, _ = self.post("/objects/e", {"at": "1577880000"})  # 2020-01-01ish
        self.assertEqual(st, 201)
        self.assertEqual(self.total("/objects/e?at=1577880000"), 1)

    def test_nanosecond_fraction_accepted(self):
        st, _, _ = self.post("/objects/e", {"at": "2020-01-01T12:00:00.123456789Z"})
        self.assertEqual(st, 201)


class TestQueryValidation(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {
            "name": "t", "fields": {"meta": "json", "n": "number"}})
        self.post("/objects/t", {"n": 1})

    def test_sort_on_json_rejected(self):
        st, _, _ = self.get("/objects/t?sort=meta")
        self.assertEqual(st, 400)

    def test_invalid_order_rejected(self):
        st, _, _ = self.get("/objects/t?sort=n&order=sideways")
        self.assertEqual(st, 400)

    def test_field_name_with_operator_suffix_rejected(self):
        st, _, _ = self.post("/schemas/objects",
                             {"name": "x", "fields": {"score__gt": "number"}})
        self.assertEqual(st, 400)


class TestAssociationGaps(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.post("/schemas/objects", {"name": "task", "fields": {"x": "string"}})
        self.u = self.post("/objects/u", {"n": "a"})[1]["_guid"]
        self.u2 = self.post("/objects/u", {"n": "b"})[1]["_guid"]
        self.t1 = self.post("/objects/task", {"x": "1"})[1]["_guid"]
        self.t2 = self.post("/objects/task", {"x": "2"})[1]["_guid"]

    def test_symmetric_false_string_not_symmetric(self):
        st, b, _ = self.post("/schemas/associations", {
            "name": "friend", "from_type": "u", "to_type": "u",
            "forward_name": "friends", "inverse_name": "friends",
            "cardinality": "many_to_many", "symmetric": "false"})
        self.assertFalse(b["symmetric"])

    def test_cardinality_tighten_revalidates(self):
        self.post("/schemas/associations", {
            "name": "owns", "from_type": "u", "to_type": "task",
            "forward_name": "tasks", "inverse_name": "owner",
            "cardinality": "many_to_many"})
        self.post("/associations", {"assoc_name": "owns", "from_guid": self.u, "to_guid": self.t1})
        self.post("/associations", {"assoc_name": "owns", "from_guid": self.u, "to_guid": self.t2})
        # u now has 2 outgoing edges; tightening to many_to_one (from <= 1) must fail
        st, _, _ = req("PUT", "/schemas/associations/owns", {
            "from_type": "u", "to_type": "task", "forward_name": "tasks",
            "inverse_name": "owner", "cardinality": "many_to_one"})
        self.assertEqual(st, 409)

    def test_typed_associations_type_checked(self):
        st, _, _ = self.get(f"/objects/task/{self.u}/associations")  # u is not a task
        self.assertEqual(st, 404)

    def test_symmetric_direction_filter(self):
        # symmetric friend + directional owns on the same node
        self.post("/schemas/associations", {
            "name": "friend", "from_type": "u", "to_type": "u",
            "forward_name": "friends", "cardinality": "many_to_many", "symmetric": True})
        self.post("/schemas/associations", {
            "name": "owns", "from_type": "u", "to_type": "task",
            "forward_name": "tasks", "inverse_name": "owner",
            "cardinality": "one_to_many"})
        self.post("/associations", {"assoc_name": "friend", "from_guid": self.u, "to_guid": self.u2})
        self.post("/associations", {"assoc_name": "owns", "from_guid": self.u, "to_guid": self.t1})
        self.assertEqual(self.total(f"/object/{self.u}/associations"), 2)
        self.assertEqual(self.total(f"/object/{self.u}/associations?direction=symmetric"), 1)
        self.assertEqual(self.total(f"/object/{self.u}/associations?direction=forward"), 1)
        self.assertEqual(self.total(f"/object/{self.u}/associations?direction=inverse"), 0)


class TestChunkedBody(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")

    def test_chunked_rejected_and_closes(self):
        s = socket.create_connection(("127.0.0.1", _PORT), timeout=5)
        body = (
            "POST /objects/x HTTP/1.1\r\nHost: x\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
            "5\r\nhello\r\n0\r\n\r\n"
        ).encode()
        s.sendall(body)
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
        self.assertIn(b"400", data.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", data)
        self.assertNotIn(b"501", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
