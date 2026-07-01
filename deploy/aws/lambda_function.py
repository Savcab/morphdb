"""AWS Lambda adapter for MorphDB (additive deploy glue — no core code changes).

This is the thin HTTP shim that :mod:`morphdb.server` already implements for the
stdlib HTTP server, re-expressed for a Lambda **Function URL** (payload format
2.0). It reuses the real dispatch core verbatim:

    routes.dispatch(method, path, query, body, headers) -> (status, payload)

— the same route lookup + handler invocation the stdlib server runs. No business
logic lives here. The persistence target comes from
``$MORPHDB_DATABASE_URL`` (a Postgres or DynamoDB URL); the schema is created
on the first (cold-start) invocation and the backend handle is reused while the
execution environment stays warm, with a one-shot reconnect if it goes stale.

NOTE: there is intentionally no authentication here — the Function URL is public.
Only deploy this in front of a database you are comfortable exposing for testing.
"""

import base64
import json
import os
import urllib.parse
from email.message import Message

from morphdb import db
from morphdb.errors import ApiError
from morphdb.routes import dispatch

# CORS is handled by the Lambda Function URL's own CORS config (the single
# source of truth). The app deliberately does NOT emit CORS headers — emitting
# them here too would duplicate Access-Control-Allow-Origin, which browsers
# reject ("multiple values ... only one is allowed").
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_INITED = False


def _ensure_db():
    """Initialise the backend once per warm execution environment."""
    global _INITED
    if not _INITED:
        target = os.environ.get("MORPHDB_DATABASE_URL")
        if not target:
            raise RuntimeError("MORPHDB_DATABASE_URL is not set on the function.")
        db.init_db(target)
        _INITED = True


def _ci_headers(raw):
    """Case-insensitive header lookup. The Function URL lower-cases header names,
    but handlers read canonical names like 'X-App-Key'; ``email.message.Message``
    matches headers case-insensitively (stdlib), so 'X-App-Key' finds 'x-app-key'.
    """
    m = Message()
    for k, v in raw.items():
        m[k] = v
    return m


def _response(status, payload):
    try:
        body = json.dumps(payload, default=str, allow_nan=False)
    except (TypeError, ValueError):
        status, body = 500, json.dumps(
            {"error": {"code": "serialization_error",
                       "message": "Result was not JSON-serializable."}})
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": body}


def _run(method, path, query, body, headers):
    _ensure_db()
    status, payload = dispatch(method, path, query, body, headers)
    return _response(status, payload)


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    path = http.get("path") or event.get("rawPath") or "/"
    query = {
        k: v[-1]
        for k, v in urllib.parse.parse_qs(
            event.get("rawQueryString") or "", keep_blank_values=True).items()
    }
    headers = _ci_headers(event.get("headers") or {})

    if method == "OPTIONS":
        # Preflight is normally answered by the Function URL CORS layer before
        # reaching here; respond bare just in case.
        return {"statusCode": 204, "headers": {}, "body": ""}

    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    try:
        body = json.loads(raw) if (raw and method in _BODY_METHODS) else {}
    except (json.JSONDecodeError, ValueError) as e:
        return _response(400, {"error": {"code": "bad_request",
                                         "message": f"Invalid JSON body: {e}"}})

    try:
        return _run(method, path, query, body, headers)
    except ApiError as e:
        return _response(e.status, e.to_dict())
    except Exception:  # noqa: BLE001 — maybe a stale pooled connection; retry once
        global _INITED
        _INITED = False
        try:
            return _run(method, path, query, body, headers)
        except ApiError as e:
            return _response(e.status, e.to_dict())
        except Exception as e2:  # noqa: BLE001 — last-resort guard
            return _response(500, {"error": {"code": "internal_error",
                                             "message": str(e2)}})
