"""Background-service management for the MorphDB server.

Starts ``python -m morphdb`` as a detached child process (its own session, so
it survives the terminal closing — the zero-dependency equivalent of a tmux
session), and records pid + bind info under the state dir so ``status``/``stop``
can find it later. Pure stdlib.

The server and the read-only dashboard are two instances of the *same* daemon
lifecycle (detached child + pid/meta/log under the state dir), so both run on one
small :class:`_Daemon` helper, parameterized by their meta/log filenames, default
port, liveness probe, and launch argv.
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
DEFAULT_DASH_PORT = 8788


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


def _probe(host, port, path="/health", timeout=1.5):
    """Liveness probe: GET ``path`` and check for HTTP 200. The server answers at
    ``/health``; the dashboard has no such endpoint, so its daemon probes ``/``
    (which serves the dashboard HTML)."""
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}{path}", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _terminate(pid, timeout=5.0):
    """SIGTERM a pid, escalate to SIGKILL if it lingers. False if already gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.15)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


class _Daemon:
    """One detached background process (the server, or the dashboard).

    Owns its own pid/meta + log files under the state dir, and a liveness probe.
    ``check_port`` refuses to start if something already holds the port;
    ``clear_on_death`` drops the meta file if the child dies during startup.
    """

    def __init__(self, meta_name, log_name, default_port, probe, argv,
                 check_port=False, clear_on_death=False):
        self.meta_name = meta_name
        self.log_name = log_name
        self.default_port = default_port
        self.probe = probe            # (host, port) -> bool
        self.argv = argv              # (host, port, db) -> list[str]
        self.check_port = check_port
        self.clear_on_death = clear_on_death

    def meta_file(self):
        return _path(self.meta_name)

    def log_file(self):
        return _path(self.log_name)

    def read_meta(self):
        try:
            with open(self.meta_file()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def write_meta(self, meta):
        with open(self.meta_file(), "w") as f:
            json.dump(meta, f)

    def clear_meta(self):
        try:
            os.remove(self.meta_file())
        except OSError:
            pass

    def status(self):
        """Snapshot: {running, [pid, host, port, db, healthy] | [stale]}."""
        meta = self.read_meta()
        if not meta:
            return {"running": False}
        if not _alive(meta.get("pid")):
            return {"running": False, "stale": True, **meta}
        return {
            "running": True,
            **meta,
            "healthy": self.probe(meta.get("host", DEFAULT_HOST),
                                  meta.get("port", self.default_port)),
        }

    def start(self, host=DEFAULT_HOST, port=None, db=None, wait=6.0):
        """Start the daemon detached. Returns (status_dict, attempted_start_bool)."""
        if port is None:
            port = self.default_port
        if self.status().get("running"):
            return self.status(), False
        if self.check_port and self.probe(host, port):   # someone else holds it
            return {"running": False, "port_in_use": True,
                    "host": host, "port": port}, False
        db = resolve_target(db)
        log = open(self.log_file(), "ab")
        proc = subprocess.Popen(
            self.argv(host, port, db),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,            # detach from the controlling terminal
        )
        self.write_meta({"pid": proc.pid, "host": host, "port": port, "db": db})
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:        # died on startup (e.g. port in use)
                break
            if self.probe(host, port):
                break
            time.sleep(0.2)
        if self.clear_on_death and proc.poll() is not None:
            self.clear_meta()
        return self.status(), True

    def stop(self, timeout=5.0):
        """Stop the daemon. Returns True if a live process was killed."""
        meta = self.read_meta()
        if not meta or not _alive(meta.get("pid")):
            self.clear_meta()
            return False
        killed = _terminate(meta["pid"], timeout)
        self.clear_meta()
        return killed


_SERVER = _Daemon(
    "service.json", "server.log", DEFAULT_PORT, _probe,
    lambda host, port, db: [sys.executable, "-m", "morphdb",
                            "--host", host, "--port", str(port), "--db", db])

# The dashboard child re-invokes this very CLI as ``morphdb dashboard
# --foreground`` (via ``python -m morphdb.cli``), so it never needs the console
# script on ``$PATH``.
_DASH = _Daemon(
    "dashboard.json", "dashboard.log", DEFAULT_DASH_PORT,
    lambda host, port: _probe(host, port, "/"),
    lambda host, port, db: [sys.executable, "-m", "morphdb.cli", "dashboard",
                            "--foreground", "--host", host, "--port", str(port),
                            "--db", db],
    check_port=True, clear_on_death=True)


# --- server lifecycle (module-level API used by the CLI / tests) --------------


def log_file():
    return _SERVER.log_file()


def read_meta():
    return _SERVER.read_meta()


def write_meta(meta):
    _SERVER.write_meta(meta)


def clear_meta():
    _SERVER.clear_meta()


def status():
    return _SERVER.status()


def start(host=DEFAULT_HOST, port=DEFAULT_PORT, db=None, wait=6.0):
    return _SERVER.start(host, port, db, wait)


def stop(timeout=5.0):
    return _SERVER.stop(timeout)


def app_count(target):
    """Read-only count of registered apps (for status). None if unreadable.

    Handles both a SQLite path and a Postgres URL.
    """
    from .. import backend as bemod
    try:
        if bemod.is_url(target):
            be = bemod.from_target(target)
            raw = be.connect()
            try:
                if be.name == "dynamodb":
                    from ..storage import DynamoStorage
                    return len(DynamoStorage(raw).list_apps())
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


# --- dashboard lifecycle (parallel to the server, its own pid/meta/log) -------


def dash_log_file():
    return _DASH.log_file()


def dashboard_status():
    return _DASH.status()


def dashboard_start(host=DEFAULT_HOST, port=DEFAULT_DASH_PORT, db=None, wait=6.0):
    return _DASH.start(host, port, db, wait)


def dashboard_stop(timeout=5.0):
    return _DASH.stop(timeout)
