# DynamoDB Transactions And Consistency

Date: 2026-06-28

Status: Accepted

## Decision

Use DynamoDB transactions and conditional writes to preserve MorphDB's current
write semantics where practical.

Normal object, schema, relation, and index-maintenance writes should be atomic
when they span multiple DynamoDB items.

## Current MorphDB Semantics

SQLite/Postgres writes run inside `db.transaction()`. Multi-step writes, such as
object blob updates plus field-index rewrites plus relation updates, commit
together or roll back together.

The DynamoDB backend should preserve that behavior for normal-sized writes.

## DynamoDB Direction

- Use `TransactWriteItems` for normal multi-item writes.
- Use conditional writes for uniqueness and relation cardinality checks.
- Keep object source items and derived index/relation items consistent within the
  transaction where they fit.
- If a write exceeds DynamoDB transaction limits, fail clearly or split only when
  correctness can still be guaranteed.
- Prefer strong consistency for direct table reads where correctness matters.
- Account for the fact that GSI-backed reads are eventually consistent.

## Large Operations

Large app/type deletes, large relation rewrites, and large index backfills may
exceed the shape DynamoDB can handle as one transaction.

Initial behavior should preserve correctness and fail clearly when an operation
cannot be made atomic. Later work can introduce explicit background jobs or
repair/status tracking for very large operations.

## Documentation

Backend performance documentation should explain that DynamoDB transactions have
API limits and that large mutation operations may be more constrained than the
SQLite/Postgres equivalents.
