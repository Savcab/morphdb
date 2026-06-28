"""Test configuration.

By default the suite runs against in-memory SQLite (zero dependencies). Set
``MORPHDB_TEST_DATABASE_URL`` to a Postgres or DynamoDB URL to run the public
engine tests against that backend instead: every ``db.init_db(":memory:")`` is
redirected to a freshly wiped backend target, so tests stay isolated and
order-independent.

    MORPHDB_TEST_DATABASE_URL=postgresql://localhost/morphdb_test \
        python -m pytest tests/

    MORPHDB_TEST_DATABASE_URL='dynamodb://morphdb-test?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true' \
        python -m pytest tests/

A handful of tests assert SQL/SQLite-specific behavior (the query planner via
``EXPLAIN QUERY PLAN``, raw SQL table inspection, and local sqlite-file/daemon
management) and are skipped on non-SQLite backends; everything else must pass
identically.
"""

import os

import pytest

from morphdb import backend
from morphdb import db

_TARGET = os.environ.get("MORPHDB_TEST_DATABASE_URL")
_BACKEND = backend.from_target(_TARGET).name if _TARGET else "sqlite"

if _TARGET:
    _orig_init = db.init_db

    def _init_db(target):
        # Each test calls init_db(":memory:") for a fresh DB; on managed backends
        # that means wipe + recreate the target. Real paths/URLs pass through.
        if target == ":memory:":
            return db._reset_and_init(_TARGET)
        return _orig_init(target)

    db.init_db = _init_db

    # Tests that can only pass on SQL/SQLite.
    if _BACKEND == "postgres":
        _SKIP_NODES = (
            "TestIndexBacked",                       # EXPLAIN QUERY PLAN is SQLite syntax
            "test_relation_filter_is_index_backed",  # also EXPLAIN QUERY PLAN
        )
        _SKIP_FILES = ("test_cli.py",)            # local sqlite-file / daemon management
    else:
        _SKIP_NODES = (
            "TestDeleteAppCascade",                 # inspects SQL tables directly
            "test_relation_filter_is_index_backed", # EXPLAIN QUERY PLAN
        )
        _SKIP_FILES = (
            "test_cli.py",                          # local sqlite-file / daemon management
            "test_field_index.py",                  # raw field_index SQL assertions
        )

    def pytest_collection_modifyitems(config, items):
        skip = pytest.mark.skip(
            reason=f"SQLite/SQL-specific; not run against the {_BACKEND} backend")
        for item in items:
            nid = item.nodeid
            if any(s in nid for s in _SKIP_NODES) or \
                    any(f in nid for f in _SKIP_FILES):
                item.add_marker(skip)
