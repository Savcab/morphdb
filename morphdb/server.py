"""Dependency-free HTTP server built on the standard library.

ThreadingHTTPServer + a small dispatch shim. Every response carries permissive
CORS headers so browser frontends served from any origin (file://, a Vite dev
server, etc.) can call the API directly. All DB access is serialized by a lock
in :mod:`morphdb.db`, so threaded request handling is safe.
"""

import json
import os
import socket
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import apps
from . import db
from . import streams
from .errors import ApiError, bad_request, not_found
from .routes import dispatch

MAX_BODY = 16 * 1024 * 1024  # 16 MB cap to avoid runaway memory on bad input
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class Handler(BaseHTTPRequestHandler):
    server_version = "MorphDB/0.1"
    protocol_version = "HTTP/1.1"  # keep-alive; requires correct Content-Length

    # -- helpers --------------------------------------------------------------

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-App-Key")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, payload):
        try:
            body = json.dumps(payload, default=str, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = json.dumps(
                {"error": {"code": "serialization_error",
                           "message": "Result was not JSON-serializable."}}
            ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # If we decided to close the connection (e.g. an unread body), tell the
        # client so it doesn't reuse a desynced keep-alive socket.
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self._set_cors()
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _read_raw_body(self):
        """Read (drain) the request body for ANY method and return the bytes.

        Draining regardless of method is essential: an unread body on a
        keep-alive connection would be misparsed as the next request.
        """
        te = self.headers.get("Transfer-Encoding", "")
        if te and te.strip().lower() != "identity":
            # We don't decode chunked bodies; we also can't know their length to
            # drain them, so close the connection to avoid a keep-alive desync.
            self.close_connection = True
            raise ApiError(400, "bad_request",
                           "Transfer-Encoding is not supported; send a body with "
                           "Content-Length.")
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            # We cannot know how many bytes to drain — close to avoid desyncing
            # the next request on a keep-alive connection.
            self.close_connection = True
            raise ApiError(400, "bad_request", "Invalid Content-Length header.")
        if n < 0:
            # A negative length is invalid and leaves the body undrained; close
            # the connection so leftover bytes can't be misread as the next req.
            self.close_connection = True
            raise ApiError(400, "bad_request", "Invalid Content-Length header.")
        if n == 0:
            return b""
        if n > MAX_BODY:
            # Don't read a potentially huge body; close the connection so the
            # unread bytes can't be misread as the next request.
            self.close_connection = True
            raise ApiError(413, "payload_too_large",
                           f"Request body exceeds {MAX_BODY} bytes.")
        return self.rfile.read(n)

    def _parse_body(self, raw):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError,
                ValueError) as e:
            # Body was fully read, so the connection stays in sync.
            raise ApiError(400, "bad_request", f"Invalid JSON body: {e}")

    # -- dispatch -------------------------------------------------------------

    def _dispatch(self):
        # Always drain the body first, for every method, so a stray body on a
        # GET/HEAD/OPTIONS request cannot desync a reused keep-alive connection.
        try:
            raw = self._read_raw_body()
        except ApiError as e:
            self._send_json(e.status, e.to_dict())
            return

        if self.command == "OPTIONS":
            self.send_response(204)
            self._set_cors()
            self.send_header("Content-Length", "0")
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            self.end_headers()
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = {
            k: v[-1]
            for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        }

        # SSE streaming is served here, off the request/response path, because it
        # holds the socket open. Everything else goes through dispatch().
        if self.command == "GET" and path.startswith("/stream/"):
            self._stream(path[len("/stream/"):], query)
            return

        try:
            body = self._parse_body(raw) if self.command in _BODY_METHODS else {}
            status, payload = dispatch(self.command, path, query, body, self.headers)
            self._send_json(status, payload)
        except ApiError as e:
            self._send_json(e.status, e.to_dict())
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — last-resort guard
            traceback.print_exc()
            self._send_json(
                500, {"error": {"code": "internal_error", "message": str(e)}}
            )

    # -- SSE streaming --------------------------------------------------------

    def _resolve_app(self, query):
        """App key for a stream: ?app_key= (EventSource can't set headers) or the
        X-App-Key header. Same validation/errors as require_app."""
        key = query.get("app_key") or self.headers.get("X-App-Key")
        if key is None or not key.strip():
            raise bad_request(
                "Missing app key. Pass ?app_key=<key> (EventSource cannot set "
                "headers) or the X-App-Key header.")
        key = key.strip()
        apps.validate_app_key(key)
        if not apps.app_exists(key):
            raise not_found(f"Unknown app '{key}'. Register it first with POST /app.")
        return key

    def _stream(self, type_name, query):
        q = dict(query)
        mode = q.pop("mode", "snapshot")
        refresh = q.pop("refresh", None)
        q.pop("app_key", None)
        from .objects import DEFAULT_LIMIT
        limit = q.pop("limit", DEFAULT_LIMIT)
        offset = q.pop("offset", 0)
        sort = q.pop("sort", None)
        order = q.pop("order", "asc")
        include = q.pop("include", None)
        # everything left in q is a field/relation filter
        try:
            app = self._resolve_app(query)
            sub = streams.attach(app, type_name, filters=q, limit=limit,
                                 offset=offset, sort=sort, order=order,
                                 include=include, mode=mode, refresh=refresh)
        except ApiError as e:
            # Fails before any event flows, as a normal JSON error — EventSource
            # surfaces it as a terminal error instead of retrying forever.
            self._send_json(e.status, e.to_dict())
            return
        self._pump(sub)

    def _pump(self, sub):
        """Drive one subscription's frames to the socket until it closes or the
        peer goes away. Nagle off (SSE frames are the small-write pattern it
        penalizes); a per-socket send timeout reaps black-holed peers."""
        self.close_connection = True     # close-delimited; no keep-alive reuse
        # The whole pump, INCLUDING header emission, is inside the try/finally
        # that owns detach — a peer that drops during the header write must not
        # leak the already-registered subscription (and its cap slot).
        try:
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.settimeout(streams.KNOBS.send_timeout)
            except OSError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._set_cors()
            self.end_headers()
            self.wfile.write(streams._RETRY_FRAME)
            self.wfile.flush()
            while True:
                frame, terminal = sub.next_frame(streams.KNOBS.heartbeat)
                if frame is streams.HEARTBEAT:
                    self.wfile.write(streams.HEARTBEAT_FRAME)
                elif frame is streams.CLOSED:
                    break
                else:
                    self.wfile.write(frame)
                self.wfile.flush()
                if terminal:
                    break
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            streams.detach(sub)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch
    do_HEAD = _dispatch

    def log_message(self, fmt, *args):
        if os.environ.get("MORPHDB_QUIET"):
            return
        sys.stderr.write("[morphdb] %s %s\n" % (self.address_string(), fmt % args))


class MorphServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host="127.0.0.1", port=8787, db_path=None):
    # Target precedence: explicit --db, then $MORPHDB_DATABASE_URL (a Postgres
    # URL or a path), then a local SQLite file. init_db routes path vs URL.
    target = db_path or os.environ.get("MORPHDB_DATABASE_URL") or "morphdb.sqlite3"
    db.init_db(target)
    # This transport can hold connections open, so it opts into streaming: the
    # capability flag flips true and the stream workers start. Lambda/embedders
    # that go through dispatch() never do, and honestly report streaming:false.
    streams.STREAMING = True
    streams.start()
    httpd = MorphServer((host, port), Handler)
    engine = db.engine()
    where = engine.describe() if engine is not None else target
    sys.stderr.write(
        f"MorphDB v{__import__('morphdb').__version__} listening on "
        f"http://{host}:{port}  ({engine.name if engine else '?'}: {where})\n"
        f"Try:  curl http://{host}:{port}/help\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[morphdb] shutting down\n")
    finally:
        streams.stop()
        httpd.server_close()
