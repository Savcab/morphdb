# DynamoDB Sorting

Date: 2026-06-28

Status: Accepted

## Decision

Preserve MorphDB's existing sorting behavior for the initial DynamoDB backend.

The DynamoDB backend should support the same public sort options as
SQLite/Postgres:

- `_created_at`
- `_updated_at`
- `_guid`
- declared scalar fields with `"index": true`

It should continue to reject unsupported sorts such as unindexed fields, `json`
fields, and relations.

## DynamoDB Caveat

DynamoDB can efficiently return sorted results only when the requested order
matches the sort key of the table or index being queried. It does not provide a
general SQL-style `ORDER BY` over arbitrary candidate rows.

Therefore, some MorphDB sort requests may require the DynamoDB backend to gather
candidate items and sort them in application code before applying `limit` and
`offset`. This preserves correctness, but it can be expensive.

## Optimization Direction

Optimize common sort paths where the single-table/index layout can support them
directly:

- natural object listing by app/type/created time
- `_created_at` and `_guid` where practical
- indexed scalar field sorts when generic field-index records can provide the
  correct order

Fall back to application-side sorting only when required for API parity.

## Documentation And Future Work

Backend performance documentation should flag expensive sort patterns for
DynamoDB.

Future performance-optimized mode may reject, warn on, or require explicit opt-in
for sort patterns that require broad reads and application-side sorting.
