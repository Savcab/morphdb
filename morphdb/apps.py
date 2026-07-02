"""Apps — the multi-tenant root.

One MorphDB process can back many independent websites. Each is an **app**,
identified by a unique *key* the client picks at registration (no UUID needed —
any reasonable string). Every schema and object lives under exactly one app and
is invisible to the others, so two apps may reuse the same type names without
colliding.

Wire protocol: every schema and object request carries its app via the
``X-App-Key`` HTTP header. A request with a missing or unknown key is refused —
there is no global, app-less namespace.

By design there is **no "list all apps" endpoint**: an agent only ever talks to
an app whose key it already knows (the key it registered). The surface is just
two calls, ``POST /app`` (register) and ``DELETE /app/{key}`` (delete + cascade).
"""

import re

from . import db
from .errors import bad_request, conflict, not_found
from .util import now_iso

# Header- and path-safe: letters, digits, '.', '_', '-'; must start
# alphanumeric; 1–128 chars. \Z (not $) so a trailing newline can't sneak in.
_APP_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_app_key(key):
    if not isinstance(key, str) or not _APP_KEY_RE.match(key):
        raise bad_request(
            "Invalid app key. Use 1-128 characters from letters, digits, '.', "
            "'_', or '-', starting with a letter or digit."
        )
    return key


def register_app(key):
    """Create a new app under ``key``. Rejects (409) if the key already exists."""
    validate_app_key(key)
    with db.store_transaction() as s:
        if s.app_exists(key):
            raise conflict(
                f"App '{key}' already exists. Pick a different, unused key."
            )
        s.create_app(key, now_iso())
    return {"key": key, "created": True}


def delete_app(key):
    """Delete an app and (via ON DELETE CASCADE) all of its schemas, objects,
    relationship definitions, and edges. Other apps are untouched.
    """
    with db.store_transaction() as s:
        if not s.app_exists(key):
            raise not_found(f"No app '{key}'.")
        s.delete_app(key)
    return {"deleted": key}


def app_exists(key):
    return db.store().app_exists(key)


def require_app(req):
    """Resolve and validate the app key for a schema/object request.

    Reads the ``X-App-Key`` header, checks its format, and confirms the app is
    registered. Raises 400 if the header is missing/malformed, 404 if the app is
    unknown. Returns the validated key for the handler to scope its work to.
    """
    key = req.headers.get("X-App-Key")
    if key is None or not key.strip():
        raise bad_request(
            "Missing X-App-Key header. Register an app with POST /app, then send "
            "its key as X-App-Key on every schema and object request."
        )
    key = key.strip()
    validate_app_key(key)
    if not app_exists(key):
        raise not_found(
            f"Unknown app '{key}'. Register it first with POST /app."
        )
    return key
