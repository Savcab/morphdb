"""Database engine abstraction — target SQLite, PostgreSQL, or DynamoDB.

MorphDB was born talking SQLite directly. This module pulls that coupling into a
single seam so the engine can persist to either:

  * **SQLite** (default, zero-dependency) — an embedded file, exactly as before.
  * **PostgreSQL** (optional: ``pip install morphdb[postgres]``) — a networked,
    managed database (RDS / Neon / Supabase / a plain server). The MorphDB process
    becomes a stateless API tier; the durable state lives in Postgres.

The rest of the codebase keeps writing ONE dialect of SQL — SQLite-flavored:
``?`` placeholders, ``INSERT OR IGNORE`` — and the engine translates it per
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
from dataclasses import dataclass
from abc import ABC, abstractmethod
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlparse


def is_url(target):
    """True if ``target`` is a network/cloud engine URL (vs a SQLite path)."""
    return isinstance(target, str) and (
        target.startswith("postgresql://")
        or target.startswith("postgres://")
        or target.startswith("dynamodb://"))


def from_target(target=None):
    """Build a database engine from a target.

    ``target`` may be a Postgres URL, a SQLite file path, ``":memory:"``, or
    ``None`` — in which case ``$MORPHDB_DATABASE_URL`` is consulted, else error.
    """
    if target is None:
        target = os.environ.get("MORPHDB_DATABASE_URL")
        if not target:
            raise ValueError(
                "No database target given and $MORPHDB_DATABASE_URL is unset.")
    if is_url(target):
        if target.startswith("dynamodb://"):
            return DynamoEngine(target)
        return PostgresEngine(target)
    return SqliteEngine(target)


def adapt_params(params):
    """Normalize bind parameters across engines.

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
    """Engine-agnostic connection facade used throughout MorphDB.

    ``execute`` / ``executemany`` accept the engine's SQLite-flavored SQL; the
    engine translates it, parameters are adapted, and every call is serialized
    by the shared reentrant lock so the one underlying DB-API connection is safe
    to use from the server's request threads.
    """

    def __init__(self, engine, raw, lock):
        self.engine = engine
        self.backend = engine  # backwards-compatible attribute name
        self.raw = raw
        self._lock = lock

    def execute(self, sql, params=()):
        sql2 = self.engine.translate(sql)
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
        sql2 = self.engine.translate(sql)
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


# --- the engine interface -----------------------------------------------------


class DatabaseEngine(ABC):
    """The contract every storage engine must satisfy.

    The rest of MorphDB only ever touches an engine through this interface (plus
    the :class:`Connection` facade), so SQLite, PostgreSQL, and any future engine
    are fully interchangeable: adding one means subclassing this and implementing
    each method — nothing else in the codebase changes. Subclassing also makes the
    contract *enforced*, not just hoped-for: Python refuses to instantiate a
    engine that leaves any abstract method unimplemented.
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


class SqliteEngine(DatabaseEngine):
    """The default, zero-dependency engine: an embedded SQLite file."""

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


class PostgresEngine(DatabaseEngine):
    """Optional engine targeting PostgreSQL via psycopg (v3).

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


# --- DynamoDB ----------------------------------------------------------------


@dataclass
class DynamoRaw:
    """Small raw handle for the logical DynamoDB engine."""

    resource: object
    client: object
    table: object
    table_name: str

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class DynamoEngine(DatabaseEngine):
    """Optional engine targeting one DynamoDB table via boto3.

    Requires ``pip install morphdb[dynamodb]``. Unlike the SQL engines, the
    engine talks to DynamoDB through :mod:`morphdb.storage`'s logical methods,
    so SQL translation/introspection methods are only present for tooling.
    """

    name = "dynamodb"

    def __init__(self, url):
        self.url = url
        parsed = urlparse(url)
        table = unquote(parsed.netloc or parsed.path.lstrip("/"))
        if not table:
            raise ValueError(
                "DynamoDB URL must include a table name, e.g. "
                "dynamodb://morphdb?region=us-east-1")
        q = parse_qs(parsed.query)
        self.table_name = table
        self.region = _one(q, "region") or os.environ.get("AWS_REGION")
        self.endpoint_url = _one(q, "endpoint_url")
        self.profile = _one(q, "profile")
        self.create_table_flag = _truthy(_one(q, "create_table"))

    def describe(self):
        parts = [f"dynamodb://{self.table_name}"]
        opts = []
        if self.region:
            opts.append(f"region={self.region}")
        if self.endpoint_url:
            opts.append(f"endpoint_url={self.endpoint_url}")
        if self.create_table_flag:
            opts.append("create_table=true")
        return parts[0] + (("?" + "&".join(opts)) if opts else "")

    def connect(self):
        try:
            import boto3
        except ImportError as e:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "DynamoDB support needs boto3. Install it with:\n"
                "    pip install 'morphdb[dynamodb]'\n"
                f"(import error: {e})")
        session_kwargs = {}
        if self.profile:
            session_kwargs["profile_name"] = self.profile
        if self.region:
            session_kwargs["region_name"] = self.region
        session = boto3.Session(**session_kwargs)
        resource_kwargs = {}
        client_kwargs = {}
        if self.endpoint_url:
            resource_kwargs["endpoint_url"] = self.endpoint_url
            client_kwargs["endpoint_url"] = self.endpoint_url
        resource = session.resource("dynamodb", **resource_kwargs)
        client = session.client("dynamodb", **client_kwargs)
        table = resource.Table(self.table_name)
        return DynamoRaw(resource, client, table, self.table_name)

    def translate(self, sql):
        return sql

    def like_ci(self):
        return "LIKE"

    def create_schema(self, raw, schema_sql):
        if self.create_table_flag:
            self._create_table_if_missing(raw)
            self._validate_table(raw.client.describe_table(TableName=raw.table_name)["Table"])
            return
        try:
            desc = raw.client.describe_table(TableName=raw.table_name)
        except raw.client.exceptions.ResourceNotFoundException as e:
            raise RuntimeError(
                f"DynamoDB table '{raw.table_name}' does not exist. Create it "
                "ahead of time for production, or add ?create_table=true for "
                "local/prototype use.") from e
        self._validate_table(desc["Table"])

    def _create_table_if_missing(self, raw):
        try:
            raw.client.describe_table(TableName=raw.table_name)
            return
        except raw.client.exceptions.ResourceNotFoundException:
            pass
        raw.client.create_table(
            TableName=raw.table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
                {"AttributeName": "gsi2pk", "AttributeType": "S"},
                {"AttributeName": "gsi2sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_app",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                {
                    "IndexName": "by_type_updated",
                    "KeySchema": [
                        {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        raw.table.wait_until_exists()

    def _validate_table(self, table):
        key_schema = {k["AttributeName"]: k["KeyType"] for k in table.get("KeySchema", [])}
        if key_schema.get("pk") != "HASH" or key_schema.get("sk") != "RANGE":
            raise RuntimeError(
                f"DynamoDB table '{self.table_name}' must use pk/sk as its "
                "HASH/RANGE primary key.")
        gsis = {g["IndexName"] for g in table.get("GlobalSecondaryIndexes", [])}
        missing = {"by_app", "by_type_updated"} - gsis
        if missing:
            raise RuntimeError(
                f"DynamoDB table '{self.table_name}' is missing required GSI(s): "
                f"{', '.join(sorted(missing))}.")

    def reset(self, raw):
        items = []
        while True:
            res = raw.table.scan(ProjectionExpression="pk, sk")
            items.extend(res.get("Items", []))
            if "LastEvaluatedKey" not in res:
                break
        with raw.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key=item)

    @contextmanager
    def transaction(self, raw):
        yield

    def get_user_version(self, raw):
        return 1

    def set_user_version(self, raw, version):
        pass

    def table_columns(self, raw, table):
        logical = {
            "apps": ["key", "created_at"],
            "object_schemas": ["app", "name", "fields", "created_at", "updated_at"],
            "objects": ["guid", "app", "object_type", "data", "created_at", "updated_at"],
            "field_index": ["app", "object_id", "object_type", "field_name", "value"],
            "association_schemas": [
                "app", "name", "from_type", "to_type", "forward_name",
                "inverse_name", "cardinality", "symmetric", "created_at", "updated_at",
            ],
            "associations": ["id", "app", "assoc_name", "from_guid", "to_guid", "created_at"],
        }
        return logical.get(table, [])

    def list_tables(self, raw):
        return [
            "apps", "object_schemas", "objects", "field_index",
            "association_schemas", "associations",
        ]


def _one(query, key):
    vals = query.get(key)
    return vals[0] if vals else None


def _truthy(v):
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# Backwards-compatible names for callers that imported the older backend terms.
Backend = DatabaseEngine
SqliteBackend = SqliteEngine
PostgresBackend = PostgresEngine
DynamoBackend = DynamoEngine
