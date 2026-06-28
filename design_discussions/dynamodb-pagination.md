# DynamoDB Pagination

Date: 2026-06-27

Status: Accepted

## Decision

For the initial DynamoDB backend, support MorphDB's existing `limit`/`offset`
pagination API only.

## Rationale

This preserves public API parity with the current SQLite/Postgres behavior and
keeps the first DynamoDB implementation focused.

DynamoDB natively paginates with cursor-like `LastEvaluatedKey` values. To
emulate large offsets, the DynamoDB backend may need to walk pages internally
until it reaches the requested offset. This is correct but can be expensive.

## Documentation Guidance

Documentation should make this clear to AI agents and app builders:

- offset pagination is acceptable for small prototype/admin datasets
- large offsets can be inefficient on DynamoDB
- DynamoDB-native apps usually prefer next/load-more/infinite-scroll UX backed by
  cursors rather than arbitrary "jump to page N" navigation
- page-number UIs on DynamoDB should be used only when the data set is known to
  be small or when the application accepts the traversal cost

## Future Work

Revisit pagination across all MorphDB backends:

- add cursor pagination as an API option
- support cursor pagination for SQLite/Postgres as keyset pagination
- map DynamoDB cursors to `LastEvaluatedKey`
- preserve offset for compatibility and page-number UI use cases
- consider caching page cursors so page-number navigation can jump to previously
  discovered pages without re-traversing from the beginning
