# DynamoDB Stacked PR Plan

Date: 2026-06-28

Status: Accepted

## Decision

Implement DynamoDB support as a stack of focused PRs.

## PR 1: Logical Storage Interface

Introduce the logical MorphDB storage interface and move the existing
SQLite/Postgres behavior onto it.

Goals:

- establish the new storage abstraction boundary
- preserve existing SQLite behavior
- preserve existing Postgres behavior
- add shared storage contract tests
- keep all existing public behavior tests passing
- avoid DynamoDB-specific implementation details in this PR

## PR 2: DynamoDB Backend

Add the DynamoDB storage implementation behind the logical storage interface.

Goals:

- add `morphdb[dynamodb]`
- support `dynamodb://...` targets through `MORPHDB_DATABASE_URL`
- implement DynamoDB table verification/optional creation
- implement DynamoDB-native object/schema/index/relation storage
- pass shared storage contract tests where DynamoDB is enabled
- add optional DynamoDB Local/LocalStack integration test path

The exact concrete `PK`/`SK`/GSI key schema should be finalized in this PR. The
prior design decisions already establish the direction: one table per deployment,
fixed GSIs, generic field-index items, generic relation-index items, and no
per-field GSIs.

## PR 3: Deploy Docs And Dashboard

Add operational polish once the backend exists.

Goals:

- add AWS Lambda + DynamoDB deployment docs
- add IAM policy examples
- document DynamoDB performance caveats for AI agents and humans
- support DynamoDB logical views in the dashboard
- keep SQL raw-table exploration SQL-specific until a DynamoDB raw/logical item
  explorer is deliberately designed

## Rationale

Stacking the work keeps the core abstraction refactor reviewable, then adds the
new backend, then adds operational/documentation surfaces. This avoids one giant
PR that mixes abstraction, DynamoDB behavior, deployment docs, and dashboard UI.
