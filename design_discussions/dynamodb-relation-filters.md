# DynamoDB Relation Filters

Date: 2026-06-28

Status: Accepted

## Decision

Preserve MorphDB's existing relation filter behavior for the initial DynamoDB
backend.

Supported public API examples should match SQLite/Postgres:

```http
GET /objects/task?assignee=user_123
GET /objects/task?assignee__in=user_1,user_2
GET /objects/task?assignee__ne=user_123
GET /objects/task?assignee__exists=true
```

## DynamoDB Caveat

Positive relation lookups map naturally to DynamoDB index records. Negative
relation filters and `exists=false` are more expensive because they require
set-subtraction from a base object set.

Potentially efficient:

- relation equality
- relation `in`
- `exists=true`
- relation projection for a page of objects
- include hydration after relation lookup

Potentially expensive:

- `ne`
- `exists=false`
- broad symmetric relation lookups if the edge/index layout does not cover both
  directions directly

## Implementation Direction

Store edge/index records that support both directions needed by MorphDB:

- object-to-neighbors for projecting relations onto objects
- neighbor-to-objects for relation filters

Optimize positive relation filters first. Implement negative and existence-false
filters through base object reads plus relation-index set subtraction when
needed.

Preserve current relation semantics, including cardinality behavior and symmetric
edge canonicalization.

## Documentation And Future Work

Backend performance documentation should distinguish efficient positive relation
filters from expensive negative relation filters.

Future performance-optimized mode may warn on, reject, or require explicit opt-in
for relation filters that require broad set subtraction.
