# DynamoDB Contains Filters

Date: 2026-06-28

Status: Accepted

## Decision

Preserve MorphDB's existing `contains` filter behavior for the initial DynamoDB
backend.

The DynamoDB backend should support the same public API shape as SQLite/Postgres:

```http
GET /objects/task?title__contains=foo
```

## DynamoDB Caveat

DynamoDB can evaluate `contains` as a filter expression, but it is not a true
substring index. Filters are applied after DynamoDB reads candidate items, so a
broad `contains` query can be scan-heavy.

## Implementation Direction

Preserve correctness first:

- keep case-insensitive substring semantics aligned with SQLite/Postgres
- use an efficient base access path when another filter or app/type query can
  narrow the candidate set
- apply `contains` as post-filtering when needed
- if `contains` is the only filter, expect app/type scanning behavior

## Documentation And Future Work

Backend performance documentation should mark `contains` as potentially
expensive on DynamoDB.

Future performance-optimized mode may warn on, reject, or require explicit opt-in
for `contains` queries that would require broad scans.
