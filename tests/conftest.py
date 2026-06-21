"""Test configuration.

By default the suite runs against in-memory SQLite (zero dependencies). Set
``MORPHDB_TEST_DATABASE_URL`` to a Postgres URL to run the engine tests against
PostgreSQL instead: every ``db.init_db(":memory:")`` is redirected to a freshly
wiped Postgres schema, so tests stay isolated and order-independent.

    MORPHDB_TEST_DATABASE_URL=postgresql://localhost/morphdb_test \
        python -m pytest tests/

A handful of tests assert SQLite-specific behavior (the query planner via
``EXPLAIN QUERY PLAN``, and local sqlite-file/daemon management) and are skipped
on the Postgres backend; everything else must pass identically on both.
"""

import os

import pytest

from morphdb import db

_PG = os.environ.get("MORPHDB_TEST_DATABASE_URL")

if _PG:
    _orig_init = db.init_db

    def _init_db(target):
        # Each test calls init_db(":memory:") for a fresh DB; on Postgres that
        # means wipe + recreate the schema. Real paths/URLs pass through.
        if target == ":memory:":
            return db._reset_and_init(_PG)
        return _orig_init(target)

    db.init_db = _init_db

    # Tests that can only pass on SQLite.
    _SKIP_NODES = (
        "TestIndexBacked",                       # EXPLAIN QUERY PLAN is SQLite syntax
        "test_relation_filter_is_index_backed",  # also EXPLAIN QUERY PLAN
    )
    _SKIP_FILES = ("test_cli.py",)            # local sqlite-file / daemon management

    def pytest_collection_modifyitems(config, items):
        skip = pytest.mark.skip(
            reason="SQLite-specific; not run against the Postgres backend")
        for item in items:
            nid = item.nodeid
            if any(s in nid for s in _SKIP_NODES) or \
                    any(f in nid for f in _SKIP_FILES):
                item.add_marker(skip)
