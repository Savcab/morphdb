# DynamoDB Storage Contract Tests

Date: 2026-06-28

Status: Accepted

## Decision

Add shared storage contract tests for the logical MorphDB storage interface.

Every storage implementation should pass the same contract where the behavior is
part of MorphDB's public semantics:

- SQLite
- Postgres
- DynamoDB

## Scope

Contract tests should cover logical storage behavior directly, including:

- app registration/deletion
- object schema reads/writes
- association schema reads/writes
- object CRUD
- default-value projection semantics
- indexed field maintenance
- relation edge behavior
- list/filter/sort behavior
- reindex behavior
- transaction/atomicity expectations where applicable

## Rationale

The DynamoDB work requires a big-bang refactor from SQL-shaped storage calls to a
logical storage interface. Contract tests are the safety rail that keeps the new
interface behavior-compatible across SQLite, Postgres, and DynamoDB.

Existing HTTP/domain tests should remain, but storage contract tests make backend
failures easier to isolate.
