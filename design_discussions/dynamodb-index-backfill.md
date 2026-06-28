# DynamoDB Index Backfill

Date: 2026-06-28

Status: Accepted

## Decision

For the initial DynamoDB backend, indexed-field schema changes should perform
synchronous backfills for parity with SQLite/Postgres.

When an indexed field is added, removed, or retyped, MorphDB should update the
derived DynamoDB field-index items before the schema operation returns.

## Expected Behavior

- Adding `"index": true` backfills index items for existing objects of that type.
- Removing `"index": true` removes that field's index items.
- Retyping an indexed field rebuilds index items according to the new field type.
- Object blobs remain the source of truth.
- No DynamoDB GSI or table-level infrastructure change is created for a MorphDB
  field change.

## DynamoDB Caveat

The operation is logically synchronous, but DynamoDB writes must still respect API
limits such as batch-write and transaction limits. Large backfills may need to be
implemented as multiple internal batches while the request remains in progress.

This prioritizes immediate query correctness over lower-latency schema edits.

## Future Work

If large-app backfills become too slow for interactive use, MorphDB can revisit
async/eventual reindexing with explicit status tracking. That would be a later
behavior change and should not be implicit in the initial DynamoDB backend.
