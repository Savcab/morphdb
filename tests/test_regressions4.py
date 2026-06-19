"""Round-4 regression tests: bugs found by the third ultracode workflow."""

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


class TestRetypeLazy(Base):
    def test_string_to_number_reads_as_unset(self):
        # Purely lazy: after a retype, old-typed values read as unset (the field
        # is "not set for the new type yet"), and queries agree — no row rewrite.
        self.post("/schemas/objects", {"name": "t", "fields": {"v": "string"}})
        g1 = self.post("/objects/t", {"v": "42"})[1]["_guid"]
        self.post("/objects/t", {"v": "hello"})
        req("PUT", "/schemas/objects/t", {"merge": True, "fields": {"v": "number"}})
        st, b, _ = self.get(f"/objects/t/{g1}")
        self.assertIsNone(b["v"])                       # string "42" is not a number
        self.assertEqual(self.total("/objects/t?v=42"), 0)      # query agrees
        self.assertEqual(self.total("/objects/t?v__gt=0"), 0)   # no false matches

    def test_rewrite_sets_value_for_new_type(self):
        self.post("/schemas/objects", {"name": "s", "fields": {"v": "number"}})
        g = self.post("/objects/s", {"v": 50})[1]["_guid"]
        req("PUT", "/schemas/objects/s", {"merge": True, "fields": {"v": "string"}})
        self.assertIsNone(self.get(f"/objects/s/{g}")[1]["v"])  # 50 not a string
        req("PATCH", f"/objects/s/{g}", {"v": "fifty"})         # set for new type
        st, b, _ = self.get(f"/objects/s/{g}")
        self.assertEqual(b["v"], "fifty")
        self.assertEqual(self.total("/objects/s?v=fifty"), 1)   # query agrees


class TestDatetimeConsistency(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "e", "fields": {"at": "datetime"}})

    def test_small_json_number_rejected(self):
        # consistent with the string path: ambiguous small epoch is rejected
        for v in (0, 100, 10000000):
            st, _, _ = self.post("/objects/e", {"at": v})
            self.assertEqual(st, 400, v)

    def test_large_json_epoch_ok(self):
        st, _, _ = self.post("/objects/e", {"at": 1577880000})
        self.assertEqual(st, 201)

    def test_malformed_z_offset_rejected(self):
        for v in ("2024-01-15T10:00:00Z+05:00", "2024-01-15T10:00:00ZZ"):
            st, _, _ = self.post("/objects/e", {"at": v})
            self.assertEqual(st, 400, v)


class TestOffsetOverflow(Base):
    def test_huge_offset_is_400_not_500(self):
        self.post("/schemas/objects", {"name": "n", "fields": {"t": "string"}})
        st, _, _ = self.get("/objects/n?offset=99999999999999999999999")
        self.assertEqual(st, 400)


class TestNeighborTypeFromObject(Base):
    def test_neighbor_type_follows_actual_object(self):
        self.post("/schemas/objects", {"name": "u", "fields": {"n": "string"}})
        self.post("/schemas/objects", {"name": "task", "fields": {"x": "string"}})
        u = self.post("/objects/u", {"n": "a"})[1]["_guid"]
        t = self.post("/objects/task", {"x": "1"})[1]["_guid"]
        self.post("/schemas/associations", {
            "name": "owns", "from_type": "u", "to_type": "task",
            "forward_name": "tasks", "inverse_name": "owner",
            "cardinality": "one_to_many"})
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        st, b, _ = self.get(f"/object/{u}/associations?expand=true")
        a = b["associations"][0]
        self.assertEqual(a["neighbor_type"], a["neighbor"]["_type"])  # never diverge


class TestNegativeContentLength(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")
        # define a type so the first POST would otherwise succeed
        req("POST", "/schemas/objects", {"name": "task", "fields": {"title": "string"}})

    def test_negative_content_length_no_desync(self):
        s = socket.create_connection(("127.0.0.1", _PORT), timeout=5)
        inner = b'{"title":"x"}'
        payload = (b"POST /objects/task HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\n"
                   b"Content-Length: -1\r\n\r\n" + inner)
        payload += b"GET /help HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(payload)
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
        # must be a clean 400 + close, never a 201 then a desynced 501
        self.assertIn(b"400", data.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", data)
        self.assertNotIn(b"501", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
