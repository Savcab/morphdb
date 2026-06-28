# DynamoDB Storage Approach

Date: 2026-06-27

Status: Accepted

## Decision

Implement DynamoDB as a real MorphDB storage backend, not as raw DynamoDB
exposure.

The implementation should be correctness-first with respect to MorphDB behavior,
while using DynamoDB-native physical modeling where it helps.

## Direction

Use a single-table-style DynamoDB model with `PK`/`SK` records for objects,
schemas, relations, and derived index items. Optimize common paths such as:

- get object by guid
- list objects by app/type
- read relations for an object
- relation equality lookups
- simple indexed field equality/range queries

Avoid designing the backend as a SQL emulator at the storage level, but preserve
the public MorphDB API contract.

## Why MorphDB Still Matters

DynamoDB is schemaless storage, but MorphDB provides the agent-facing layer:

- stable generic HTTP endpoints
- schema registry
- validation and defaults
- relation/inverse modeling
- tenant isolation by app key
- filter/sort conventions
- CLI and dashboard workflows
- no browser-side AWS credentials

Raw DynamoDB would still require every vibe-coded app to invent its own API,
validation rules, relation model, access patterns, and security boundary.
