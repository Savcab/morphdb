"""Storage backend abstraction — target SQLite or PostgreSQL behind one interface.

MorphDB was born talking SQLite directly. This module pulls that coupling into a
single seam so the engine can persist to either:

  * **SQLite** (default, zero-dependency) — an embedded file, exactly as before.
  * **PostgreSQL** (optional: ``pip install morphdb[postgres]``) — a networked,
    managed database (RDS / Neon / Supabase / a plain server). The MorphDB process
    becomes a stateless API tier; the durable state lives in Postgres.

The rest of the codebase keeps writing ONE dialect of SQL — SQLite-flavored:
``?`` placeholders, ``INSERT OR IGNORE`` — and the backend translates it per
engine at execute time. That works because the data model is vanilla relational
(a JSON blob per object + a typed EAV index table + an edge table); the only real
differences are dialect surface:

    placeholders        ?                      -> %s
    upsert              INSERT OR IGNORE       -> INSERT ... ON CONFLICT DO NOTHING
    autoincrement PK    INTEGER ... AUTOINCREMENT -> BIGINT GENERATED ... AS IDENTITY
    case-insensitive    LIKE                   -> ILIKE
    schema version      PRAGMA user_version    -> a morphdb_meta table
    column introspection PRAGMA table_info     -> information_schema.columns
    booleans            (stored as 0/1 ints; a Python bool is coerced on bind)

Concurrency: as in the original design, a single connection guarded by a
reentrant lock serializes all access — simple and correct at single-instance
scale. Multiple MorphDB instances against the same Postgres each hold their own
connection and rely on Postgres (MVCC + unique constraints) for cross-process
consistency. A connection pool is a future optimization, not a correctness need.
"""

import os
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager


def is_url(target):
    """True if ``target`` is a Postgres connection URL (vs a SQLite path)."""
    return isinstance(target, str) and (
        target.startswith("postgresql://") or target.startswith("postgres://"))


def from_target(target=None):
    """Build a backend from a target.

    ``target`` may be a Postgres URL, a SQLite file path, ``":memory:"``, or
    ``None`` — in which case ``$MORPHDB_DATABASE_URL`` is consulted, else error.
    """
    if target is None:
        target = os.environ.get("MORPHDB_DATABASE_URL")
        if not target:
            raise ValueError(
                "No database target given and $MORPHDB_DATABASE_URL is unset.")
    if is_url(target):
        return PostgresBackend(target)
    return SqliteBackend(target)


def adapt_params(params):
    """Normalize bind parameters across backends.

    MorphDB stores booleans as 0/1 integers (there is no native boolean column),
    so a Python ``bool`` must bind as an ``int`` — required for Postgres' INTEGER
    columns, and a harmless no-op for SQLite (where ``bool`` already adapts to
    0/1). Everything else passes through unchanged.
    """
    if not params:
        return params
    return [int(p) if isinstance(p, bool) else p for p in params]


def _split_sql(sql):
    """Split a multi-statement DDL string into individual statements.

    Strips ``--`` line comments first (some carry a ``;`` mid-comment, which a
    naive split would mishandle), then splits on ``;``.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in no_comments.split(";") if s.strip()]


class _Result:
    """A buffered query result.

    Rows are fetched eagerly while the shared lock is held, so a bare read is
    atomic on the single shared connection. Exposes only the slice of the DB-API
    the engine actually uses.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Connection:
    """Backend-agnostic connection facade used throughout the engine.

    ``execute`` / ``executemany`` accept the engine's SQLite-flavored SQL; the
    backend translates it, parameters are adapted, and every call is serialized
    by the shared reentrant lock so the one underlying DB-API connection is safe
    to use from the server's request threads.
    """

    def __init__(self, backend, raw, lock):
        self.backend = backend
        self.raw = raw
        self._lock = lock

    def execute(self, sql, params=()):
        sql2 = self.backend.translate(sql)
        params2 = adapt_params(params)
        with self._lock:
            cur = self.raw.cursor()
            try:
                cur.execute(sql2, params2)
                rows = cur.fetchall() if cur.description is not None else []
            finally:
                cur.close()
        return _Result(rows)

    def executemany(self, sql, seq):
        seq2 = [adapt_params(p) for p in seq]
        if not seq2:
            return
        sql2 = self.backend.translate(sql)
        with self._lock:
            cur = self.raw.cursor()
            try:
                cur.executemany(sql2, seq2)
            finally:
                cur.close()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


# --- the backend interface ----------------------------------------------------


class Backend(ABC):
    """The contract every storage engine must satisfy.

    The rest of MorphDB only ever touches a backend through this interface (plus
    the :class:`Connection` facade), so SQLite, PostgreSQL, and any future engine
    are fully interchangeable: adding one means subclassing this and implementing
    each method — nothing else in the codebase changes. Subclassing also makes the
    contract *enforced*, not just hoped-for: Python refuses to instantiate a
    backend that leaves any abstract method unimplemented.
    """

    #: Short engine label ("sqlite" / "postgres"), used in status and logging.
    name = ""

    @abstractmethod
    def describe(self):
        """A human-readable, credential-safe description of the target (shown in
        status/logs/dashboard)."""

    @abstractmethod
    def connect(self):
        """Open and return a configured DB-API connection — row factory set so
        rows support ``row["col"]``, foreign keys / autocommit as the engine
        needs."""

    @abstractmethod
    def translate(self, sql):
        """Rewrite the engine's SQLite-flavored SQL (``?`` placeholders,
        ``INSERT OR IGNORE``) into this dialect, returning SQL ready to
        execute."""

    @abstractmethod
    def like_ci(self):
        """The case-insensitive ``LIKE`` keyword this engine uses for the
        ``contains`` filter (SQLite ``LIKE`` vs Postgres ``ILIKE``)."""

    @abstractmethod
    def create_schema(self, raw, schema_sql):
        """Create the MorphDB schema (idempotently) on a raw connection,
        translating any DDL the dialect needs."""

    @abstractmethod
    def reset(self, raw):
        """Drop all MorphDB state — used to give a test a clean database."""

    @abstractmethod
    def transaction(self, raw):
        """A context manager that runs its block as one committed transaction
        (rolled back on error)."""

    @abstractmethod
    def get_user_version(self, raw):
        """Return the stored schema version (0 if never set)."""

    @abstractmethod
    def set_user_version(self, raw, version):
        """Persist the schema version."""

    @abstractmethod
    def table_columns(self, raw, table):
        """An ordered list of a table's column names (``[]`` if absent)."""

    @abstractmethod
    def list_tables(self, raw):
        """The names of all base tables in the database."""


# --- SQLite -------------------------------------------------------------------


class SqliteBackend(Backend):
    """The default, zero-dependency backend: an embedded SQLite file."""

    name = "sqlite"

    def __init__(self, path):
        self.path = path

    def describe(self):
        return self.path

    def connect(self):
        import sqlite3
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")     # enforce the app cascade + FKs
        return conn

    def translate(self, sql):
        return sql                                  # the engine speaks SQLite already

    def like_ci(self):
        return "LIKE"                               # SQLite LIKE is ASCII case-insensitive

    def create_schema(self, raw, schema_sql):
        raw.executescript(schema_sql)
        raw.commit()

    def reset(self, raw):
        """Drop every MorphDB table (test isolation). Rarely used: ``:memory:``
        is already fresh, so this only matters for a reused file."""
        for t in ("field_index", "associations", "association_schemas",
                  "objects", "object_schemas", "apps", "morphdb_meta"):
            raw.execute(f"DROP TABLE IF EXISTS {t}")
        raw.commit()

    @contextmanager
    def transaction(self, raw):
        try:
            yield
            raw.commit()
        except Exception:
            raw.rollback()
            raise

    def get_user_version(self, raw):
        return raw.execute("PRAGMA user_version").fetchone()[0]

    def set_user_version(self, raw, version):
        raw.execute(f"PRAGMA user_version = {int(version)}")

    def table_columns(self, raw, table):
        # Ordered by definition (cid) so callers can render columns in order.
        rows = raw.execute(f"PRAGMA table_info({table})").fetchall()
        return [r["name"] for r in rows]

    def list_tables(self, raw):
        rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        return [r["name"] for r in rows]


# --- PostgreSQL ---------------------------------------------------------------


class PostgresBackend(Backend):
    """Optional backend targeting PostgreSQL via psycopg (v3).

    Requires ``pip install morphdb[postgres]``. The same engine SQL is translated
    to the Postgres dialect; identity columns, upserts, versioning, and column
    introspection use their Postgres equivalents.
    """

    name = "postgres"

    def __init__(self, url):
        self.url = url

    def describe(self):
        # Never echo credentials embedded in the URL.
        return re.sub(r"//[^@/]+@", "//***@", self.url)

    def connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "PostgreSQL support needs the psycopg driver. Install it with:\n"
                "    pip install 'morphdb[postgres]'\n"
                f"(import error: {e})")
        return psycopg.connect(self.url, autocommit=True, row_factory=dict_row)

    def translate(self, sql):
        s = sql
        if "INSERT OR IGNORE" in s:
            s = s.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
        # Escape any literal % (psycopg treats the query as a format string when
        # params are passed), then convert ? placeholders to %s.
        s = s.replace("%", "%%").replace("?", "%s")
        return s

    def like_ci(self):
        return "ILIKE"                              # Postgres LIKE is case-sensitive

    def _ddl(self, schema_sql):
        return schema_sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")

    def create_schema(self, raw, schema_sql):
        with raw.cursor() as cur:
            for stmt in _split_sql(self._ddl(schema_sql)):
                cur.execute(stmt)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS morphdb_meta (version INTEGER NOT NULL)")

    def reset(self, raw):
        with raw.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")

    @contextmanager
    def transaction(self, raw):
        # psycopg manages BEGIN/COMMIT/ROLLBACK; works even in autocommit mode.
        with raw.transaction():
            yield

    def get_user_version(self, raw):
        with raw.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS morphdb_meta (version INTEGER NOT NULL)")
            cur.execute("SELECT version FROM morphdb_meta LIMIT 1")
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO morphdb_meta (version) VALUES (0)")
                return 0
            return row["version"]

    def set_user_version(self, raw, version):
        with raw.cursor() as cur:
            cur.execute("UPDATE morphdb_meta SET version = %s", (int(version),))

    def table_columns(self, raw, table):
        with raw.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,))
            return [r["column_name"] for r in cur.fetchall()]

    def list_tables(self, raw):
        with raw.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")
            return [r["table_name"] for r in cur.fetchall()]
