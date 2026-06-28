# DynamoDB Migration Scope

Date: 2026-06-28

Status: Accepted

## Decision

Do not include SQLite/Postgres-to-DynamoDB migration support in the initial
DynamoDB backend scope.

## Direction

The initial DynamoDB backend targets fresh DynamoDB deployments and tables.

Reindexing within DynamoDB remains in scope because it rebuilds derived
field-index items from DynamoDB source object items. Cross-backend data migration
is a separate feature.

## Future Work

Future export/import or migration tooling may be considered independently, but
it should not block the first DynamoDB backend implementation.
