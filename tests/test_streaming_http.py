"""Streaming over real HTTP (PR3): snapshot mode end to end through the stdlib
server, plus the capability flag and the request/response 501 fallback.

Uses a ~40-line raw-socket SSE reader (EventSource is a browser API); the server
is the shared in-process harness server, with the stream workers started here.
"""

import socket
import time
import unittest

from morphdb import db, streams
from tests import harness


class SSE:
    """Minimal Server-Sent Events client over a raw socket."""

    def __init__(self, path, timeout=2.0):
        harness.ensure_server()
        self.sock = socket.create_connection(("127.0.0.1", harness.PORT), timeout)
        self.sock.settimeout(timeout)
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Accept: text/event-stream\r\nConnection: close\r\n\r\n".encode())
        self.buf = b""
        self.status = self._read_headers()

    def _read_line_block(self):
        while b"\r\n\r\n" not in self.buf and b"\n\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        sep = b"\r\n\r\n" if b"\r\n\r\n" in self.buf else b"\n\n"
        head, self.buf = self.buf.split(sep, 1)
        return head

    def _read_headers(self):
        head = self._read_line_block()
        first = head.split(b"\r\n")[0].decode()
        return int(first.split()[1])

    def event(self, timeout=2.0):
        """Next SSE event as (event, data_str, seq), or None on timeout/close."""
        self.sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            sep = None
            if b"\n\n" in self.buf:
                sep = b"\n\n"
            if sep is None:
                if time.monotonic() > deadline:
                    return None
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    return None
                if not chunk:
                    return ("_closed", None, None)
                self.buf += chunk
                continue
            block, self.buf = self.buf.split(sep, 1)
            ev = data = seq = None
            for line in block.decode().split("\n"):
                if line.startswith(": "):
                    ev = "_hb"
                elif line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                elif line.startswith("id: "):
                    seq = int(line[4:])
            if ev == "_hb" or (ev is None and data is None):
                continue      # heartbeat or the advisory retry: frame — skip
            return ev, data, seq

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class HttpBase(unittest.TestCase):
    def setUp(self):
        harness.ensure_server()
        db.init_db(":memory:")
        streams.STREAMING = True
        streams.reset()
        harness.register_app("s")
        harness.req("PUT", "/schema/task",
                    {"fields": {"title": {"type": "string"},
                                "done": {"type": "boolean", "index": True,
                                         "default": False}}}, app="s")

    def tearDown(self):
        streams.stop()
        streams.STREAMING = False

    def create(self, body):
        st, b, _ = harness.req("POST", "/objects/task", body, app="s")
        self.assertEqual(st, 201, b)
        return b


class TestSnapshotHttp(HttpBase):
    def test_init_then_snapshot(self):
        self.create({"title": "seed"})
        es = SSE("/stream/task?app_key=s&refresh=50")
        self.addCleanup(es.close)
        self.assertEqual(es.status, 200)
        ev, data, seq = es.event()
        self.assertEqual(ev, "init")
        self.assertIn('"mode": "snapshot"', data)
        self.assertIn('"total": 1', data)
        self.assertEqual(seq, 1)
        self.create({"title": "second"})
        ev, data, seq = es.event()
        self.assertEqual(ev, "snapshot")
        self.assertIn('"total": 2', data)
        self.assertEqual(seq, 2)

    def test_header_app_key_also_works(self):
        es = SSE("/stream/task?refresh=50")   # no app_key
        self.addCleanup(es.close)
        # header path: the raw client can't set it, so this must 400
        self.assertEqual(es.status, 400)

    def test_bad_filter_is_terminal_json_error(self):
        # filtering an un-indexed field fails before any event
        es = SSE("/stream/task?app_key=s&title=x")
        self.addCleanup(es.close)
        self.assertEqual(es.status, 400)

    def test_unknown_app_404(self):
        es = SSE("/stream/task?app_key=nope")
        self.addCleanup(es.close)
        self.assertEqual(es.status, 404)


class TestDeltaHttp(HttpBase):
    def test_delta_enter_leave_over_the_wire(self):
        es = SSE("/stream/task?app_key=s&done=false&mode=delta")
        self.addCleanup(es.close)
        self.assertEqual(es.status, 200)
        ev, data, seq = es.event()
        self.assertEqual(ev, "init")
        self.assertIn('"mode": "delta"', data)
        obj = self.create({"title": "x", "done": False})
        ev, data, _ = es.event()
        self.assertEqual(ev, "enter")
        self.assertIn(obj["_guid"], data)
        harness.req("PATCH", f"/objects/task/{obj['_guid']}",
                    {"done": True}, app="s")
        ev, data, _ = es.event()
        self.assertEqual(ev, "leave")
        self.assertIn(obj["_guid"], data)

    def test_delta_include_is_400(self):
        es = SSE("/stream/task?app_key=s&mode=delta&include=assignee")
        self.addCleanup(es.close)
        self.assertEqual(es.status, 400)


class TestCapability(HttpBase):
    def test_root_reports_streaming_true(self):
        st, b, _ = harness.req("GET", "/", app=None)
        self.assertTrue(b["streaming"])

    def test_dispatch_stream_is_501(self):
        # the transport-neutral fallback (what Lambda serves)
        from morphdb.routes import dispatch
        from morphdb.errors import ApiError
        with self.assertRaises(ApiError) as cm:
            dispatch("GET", "/stream/task", {"app_key": "s"}, {}, {})
        self.assertEqual(cm.exception.status, 501)


if __name__ == "__main__":
    unittest.main()
