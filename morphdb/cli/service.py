"""Background-service management for the MorphDB server.

Starts ``python -m morphdb`` as a detached child process (its own session, so
it survives the terminal closing — the zero-dependency equivalent of a tmux
session), and records pid + bind info under the state dir so ``status``/``stop``
can find it later. Pure stdlib.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def _home():
    """The MorphDB state dir: ``$MORPHDB_HOME`` or ``~/.morphdb``."""
    return os.environ.get("MORPHDB_HOME") or os.path.join(
        os.path.expanduser("~"), ".morphdb")


def state_dir():
    d = _home()
    os.makedirs(d, exist_ok=True)
    return d


def _path(name):
    return os.path.join(state_dir(), name)


def meta_file():
    return _path("service.json")


def log_file():
    return _path("server.log")


def default_db():
    """Where the local server keeps data by default: a per-user SQLite file.

    Override the file with ``--db`` (any path, or ``:memory:``) or move the whole
    state dir with ``$MORPHDB_HOME``. To persist to PostgreSQL instead of SQLite,
    pass a URL — ``--db postgresql://...`` or ``$MORPHDB_DATABASE_URL`` (see
    :func:`resolve_target`). To use a MorphDB hosted elsewhere, you instead point
    *clients* at that server's URL with ``$MORPHDB_HOST`` (see the skill).
    """
    return _path("data.sqlite3")


def resolve_target(db=None):
    """The persistence target for the local server.

    Precedence: an explicit ``--db`` value, then ``$MORPHDB_DATABASE_URL`` (a
    Postgres URL or a path), then the per-user SQLite file from
    :func:`default_db`.
    """
    return db or os.environ.get("MORPHDB_DATABASE_URL") or default_db()


def read_meta():
    try:
        with open(meta_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_meta(meta):
    with open(meta_file(), "w") as f:
        json.dump(meta, f)


def clear_meta():
    try:
        os.remove(meta_file())
    except OSError:
        pass


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health(host, port, timeout=1.0):
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def status():
    """Snapshot: {running, [pid, host, port, db, healthy] | [stale]}."""
    meta = read_meta()
    if not meta:
        return {"running": False}
    if not _alive(meta.get("pid")):
        return {"running": False, "stale": True, **meta}
    return {
        "running": True,
        **meta,
        "healthy": _health(meta.get("host", DEFAULT_HOST),
                           meta.get("port", DEFAULT_PORT)),
    }


def app_count(target):
    """Read-only count of registered apps (for status). None if unreadable.

    Handles both a SQLite path and a Postgres URL.
    """
    from .. import backend as bemod
    try:
        if bemod.is_url(target):
            raw = bemod.from_target(target).connect()
            try:
                cur = raw.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM apps")
                return cur.fetchone()["n"]
            finally:
                raw.close()
        # SQLite: open read-only so a status check never creates the file.
        c = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=1)
        try:
            return c.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        finally:
            c.close()
    except Exception:
        return None


def start(host=DEFAULT_HOST, port=DEFAULT_PORT, db=None, wait=6.0):
    """Start the server detached. Returns (status_dict, attempted_start_bool)."""
    if status().get("running"):
        return status(), False
    db = resolve_target(db)
    log = open(log_file(), "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "morphdb",
         "--host", host, "--port", str(port), "--db", db],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,            # detach from the controlling terminal
    )
    write_meta({"pid": proc.pid, "host": host, "port": port, "db": db})
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:        # died on startup (e.g. port in use)
            break
        if _health(host, port):
            break
        time.sleep(0.2)
    return status(), True


def stop(timeout=5.0):
    """Stop the background server. Returns True if a live process was killed."""
    meta = read_meta()
    if not meta or not _alive(meta.get("pid")):
        clear_meta()
        return False
    pid = meta["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_meta()
        return False
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.15)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    clear_meta()
    return True
