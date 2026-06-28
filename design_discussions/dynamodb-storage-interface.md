# DynamoDB Storage Interface

Date: 2026-06-28

Status: Accepted

## Decision

Do not implement DynamoDB by pretending it is a SQL/DB-API backend.

Instead, introduce or evolve toward a logical MorphDB storage interface that can
be implemented by SQL backends and by DynamoDB-native storage.

## Current Backend Shape

The current backend seam is SQL-shaped. MorphDB writes SQLite-flavored SQL, and
the Postgres backend translates that SQL into the Postgres dialect.

That works for SQLite/Postgres because both are relational engines with similar
query and transaction semantics.

DynamoDB does not fit that seam cleanly.

## Rejected Approach

Avoid building a fake SQL interpreter over DynamoDB for calls such as:

```python
execute("SELECT ...")
execute("INSERT ...")
execute("UPDATE ...")
```

This would be brittle, incomplete, and likely to grow into a hidden query engine
inside the backend adapter.

## Direction

Add or evolve toward a higher-level logical storage interface that exposes
MorphDB operations rather than SQL statements.

Examples of logical operations:

- create/delete app
- read/write object schema
- read/write association schema
- create/update/delete object
- list objects
- maintain field-index records
- maintain relation edge records
- project relations
- reindex

SQLite/Postgres may continue using SQL internally. DynamoDB should implement the
same logical behavior with DynamoDB-native keys, items, indexes, transactions,
and batch operations.

## Rationale

This matches the broader design principle:

```text
Same MorphDB API externally; backend-native implementation internally.
```

The storage interface should preserve MorphDB behavior without forcing DynamoDB
through a relational SQL-shaped adapter.
