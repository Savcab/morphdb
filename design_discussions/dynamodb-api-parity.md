# DynamoDB API Parity

Date: 2026-06-27

Status: Accepted

## Decision

The DynamoDB backend should target the same public MorphDB API behavior as the
SQLite/Postgres backends.

Existing app code should keep working when the storage target changes to
DynamoDB. The backend should preserve MorphDB behavior for schemas, CRUD,
validation, defaults, relations, includes, filters, sorting, exact `total`,
`limit`, and `offset`.

## Caveat

Some operations may be correct but less efficient on DynamoDB than on
SQLite/Postgres. Documentation should call these out clearly for AI agents and
app builders.

Potentially expensive patterns include:

- large `offset` values
- exact `total` on broad or filtered lists
- multiple filters that cannot all map to one DynamoDB access path
- `contains`
- negative filters such as `ne` and `exists=false`
- relation filters that require set subtraction
- arbitrary sort patterns that do not match an efficient key/index shape

## Rationale

MorphDB's value is a stable, backend-independent API for vibe-coded apps. Making
DynamoDB a narrower behavioral backend would make app generation and docs more
complicated. The preferred tradeoff is full correctness with explicit DynamoDB
performance guidance.
