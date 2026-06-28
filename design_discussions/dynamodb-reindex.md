# DynamoDB Reindex

Date: 2026-06-28

Status: Accepted

## Decision

Support MorphDB reindex behavior for the DynamoDB backend.

Reindex should rebuild derived field-index items from source object items and the
current object schemas.

## Direction

- Treat object items as the source of truth.
- Scan the relevant source object items.
- Recompute generic field-index items from object data and current schemas.
- Delete stale field-index items in the selected app/type/field scope.
- Write rebuilt index items in DynamoDB batches.
- Keep the initial implementation synchronous for consistency with indexed-field
  schema backfill behavior.

## Caveat

Large reindex operations can be slower and more expensive on DynamoDB than on
SQLite/Postgres. Documentation should flag reindex as a potentially costly repair
operation for large apps.

Future work may introduce async/background reindexing with explicit status
tracking if large-app reindexing becomes painful.
