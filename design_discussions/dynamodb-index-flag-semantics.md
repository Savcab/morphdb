# DynamoDB Index Flag Semantics

Date: 2026-06-28

Status: Accepted

## Decision

On DynamoDB, MorphDB's `"index": true` field option should keep the same public
meaning as SQLite/Postgres.

An indexed scalar field is eligible for:

- filtering
- sorting

An unindexed field should continue to reject filter/sort requests.

## Implementation Meaning

`"index": true` should not create a new DynamoDB GSI for that field.

Instead, the DynamoDB backend should maintain generic derived field-index items
for indexed scalar fields. Those index items can then be queried through the
fixed DynamoDB table/index layout.

## Rationale

This preserves MorphDB's schema-fluid behavior:

- adding an indexed field does not mutate AWS infrastructure
- changing a schema does not create or delete GSIs
- one DynamoDB table/index layout can support many dynamic app schemas
- the public API remains consistent across SQLite, Postgres, and DynamoDB
