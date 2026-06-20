"""Hardening: field-type edge cases and HTTP-layer robustness.

These port the lessons from earlier battle-testing to the new API surface.
"""

import socket
import unittest

import harness
from harness import Base, ensure_server, req


class TestNames(Base):
    def test_field_name_trailing_newline_rejected(self):
        st, _, _ = req("PUT", "/schema/bad",
                       raw_body=b'{"fields":{"city\\n":"string"}}')
        self.assertEqual(st, 400)

    def test_field_double_underscore_rejected(self):
        st, _, _ = self.put("/schema/bad", {"fields": {"a__b": "string"}})
        self.assertEqual(st, 400)

    def test_leading_underscore_rejected(self):
        st, _, _ = self.put("/schema/bad", {"fields": {"_x": "string"}})
        self.assertEqual(st, 400)

    def test_relation_name_validated(self):
        self.put_type("user", fields={"n": "string"})
        st, _, _ = self.put("/schema/task", {"relations": {
            "bad__name": {"to": "user", "cardinality": "many_to_one", "inverse": "t"}}})
        self.assertEqual(st, 400)


class TestNumbers(Base):
    def setUp(self):
        super().setUp()
        self.put_type("n", fields={"v": "number"})

    def test_nan_infinity_rejected(self):
        for raw in (b'{"v": NaN}', b'{"v": Infinity}', b'{"v": -Infinity}'):
            st, _, _ = req("POST", "/objects/n", raw_body=raw)
            self.assertEqual(st, 400, raw)

    def test_boolean_rejected_for_number(self):
        st, _, _ = self.post("/objects/n", {"v": True})
        self.assertEqual(st, 400)

    def test_large_int_exact_read(self):
        big = 2 ** 70 + 12345
        o = self.create("n", {"v": big})
        self.assertEqual(self.read("n", o["_guid"])["v"], big)

    def test_huge_int_query_no_500(self):
        self.create("n", {"v": 5})
        st, _, _ = self.get("/objects/n?v=999999999999999999999999")
        self.assertIn(st, (200, 400))      # never a 500


class TestDatetime(Base):
    def setUp(self):
        super().setUp()
        self.put_type("e", fields={"at": {"type": "datetime", "index": True}})

    def test_equivalent_forms_normalize_equal(self):
        for v in ("2020-01-01T12:00:00Z", "2020-01-01T12:00:00+00:00",
                  "2020-01-01T12:00:00"):
            self.create("e", {"at": v})
        self.assertEqual(self.total("/objects/e?at=2020-01-01T12:00:00Z"), 3)

    def test_offset_normalized_for_range(self):
        self.create("e", {"at": "2020-01-01T08:00:00+05:00"})    # == 03:00Z
        self.assertEqual(self.total("/objects/e?at__lt=2020-01-01T10:00:00Z"), 1)

    def test_epoch_seconds_ok_small_ambiguous_rejected(self):
        st, _, _ = self.post("/objects/e", {"at": 1577880000})
        self.assertEqual(st, 201)
        for bad in (0, 100, 10000000):
            st, _, _ = self.post("/objects/e", {"at": bad})
            self.assertEqual(st, 400, bad)

    def test_malformed_z_rejected(self):
        for v in ("2024-01-15T10:00:00Z+05:00", "2024-01-15T10:00:00ZZ"):
            st, _, _ = self.post("/objects/e", {"at": v})
            self.assertEqual(st, 400, v)


class TestJson(Base):
    def setUp(self):
        super().setUp()
        self.put_type("j", fields={"v": "json"})

    def test_json_nan_rejected(self):
        st, _, _ = req("POST", "/objects/j", raw_body=b'{"v": {"a":[1, NaN]}}')
        self.assertEqual(st, 400)
        self.assertEqual(self.total("/objects/j"), 0)     # type not bricked

    def test_deeply_nested_rejected_but_reasonable_ok(self):
        payload = b'{"v":' + b'{"a":' * 982 + b'1' + b'}' * 982 + b'}'
        st, _, _ = req("POST", "/objects/j", raw_body=payload)
        self.assertEqual(st, 400)
        # a reasonable depth still writes and reads back without a recursion 500
        val = cur = {}
        for _ in range(20):
            cur["a"] = {}
            cur = cur["a"]
        o = self.create("j", {"v": val})
        st, _, _ = self.get(f"/objects/j/{o['_guid']}")
        self.assertEqual(st, 200)


class TestHttpLayer(unittest.TestCase):
    def setUp(self):
        ensure_server()
        from morphdb import db
        db.init_db(":memory:")
        harness.register_app(harness.APP)
        req("PUT", "/schema/task", {"fields": {"title": "string"}})

    def test_head_and_options(self):
        st, _, _ = req("HEAD", "/health")
        self.assertEqual(st, 200)
        st, _, resp = req("OPTIONS", "/objects/task")
        self.assertEqual(st, 204)         # preflight: no content
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_huge_offset_is_400(self):
        st, _, _ = req("GET", "/objects/task?offset=99999999999999999999999")
        self.assertEqual(st, 400)

    def test_get_with_body_no_keepalive_desync(self):
        s = socket.create_connection(("127.0.0.1", harness.PORT), timeout=5)
        body = b'{"junk":"AAAA"}'
        first = ("GET /health HTTP/1.1\r\nHost: x\r\n"
                 "Content-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        second = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(first + second)
        s.settimeout(5)
        data = b""
        try:
            while data.count(b"HTTP/1.1 200") < 2 and b"501" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        self.assertNotIn(b"501", data)
        self.assertGreaterEqual(data.count(b"HTTP/1.1 200"), 2)

    def test_negative_content_length_no_desync(self):
        s = socket.create_connection(("127.0.0.1", harness.PORT), timeout=5)
        inner = b'{"title":"x"}'
        payload = (b"POST /objects/task HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Type: application/json\r\nContent-Length: -1\r\n\r\n"
                   + inner + b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
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
        self.assertIn(b"400", data.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", data)
        self.assertNotIn(b"501", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
