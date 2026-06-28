# DynamoDB Backend Design One-Pager

Date: 2026-06-28

Status: Summary of accepted DynamoDB design decisions

## Core Theme

The central tradeoff is **one stable MorphDB interface** versus **database-specific
optimized APIs**.

For the initial DynamoDB backend, MorphDB chooses the stable-interface side:
SQLite, Postgres, and DynamoDB should expose the same public MorphDB behavior.
Apps and coding agents should not need to learn a different API just because the
storage engine changed.

The DynamoDB implementation should still use DynamoDB-native storage patterns
under the hood. The rule is:

```text
Same MorphDB API externally; DynamoDB-aware layout and optimizations internally.
```

Future work may add explicit performance-optimized modes or cursor APIs, but
those should be opt-in and documented rather than accidental backend differences.

## Interface Parity First

DynamoDB should preserve current MorphDB behavior for:

- schemas, objects, validation, defaults, relations, and includes
- `limit`/`offset` pagination
- exact `total`
- sorting by system fields and indexed scalar fields
- multiple filters
- `contains`
- relation filters, including negative and existence filters
- lazy default-value semantics
- reindex and index backfill behavior

Some of these patterns are less natural on DynamoDB. The backend should still
return correct results, while documentation makes the cost visible to humans and
AI agents.

## DynamoDB-Native Internals

Use one DynamoDB table per MorphDB deployment/environment, with many MorphDB apps
inside it. In current MorphDB terms, the app key is the tenant boundary.

Use a single-table-style design with fixed GSIs and generic derived items:

- object items remain the source of truth
- generic field-index items support `"index": true`
- relation index items support object-to-neighbor and neighbor-to-object access
- do not create one DynamoDB GSI per MorphDB field
- do not mutate AWS infrastructure on every app schema change

Common paths should be efficient where practical: object lookup by guid, list by
app/type, simple indexed filters, positive relation lookups, and batch fetches.

## Expensive But Supported

The initial backend deliberately supports some patterns that may require broad
reads, set subtraction, application-side sorting, or walking DynamoDB pages:

- large offsets
- exact totals on broad/filtered queries
- arbitrary indexed-field sorts that do not match a key shape
- multiple filters that cannot share one access path
- `contains`
- `ne`, `exists=false`, and some relation filters

Docs should call these out at the endpoint/backend level so agents can choose
better UX and access patterns, such as load-more flows instead of page-number
jumps.

## Future Performance Mode

A future explicit mode such as `performance_optimized=true` may opt out of costly
parity features for DynamoDB. It could:

- omit exact totals
- prefer cursor pagination
- expose `has_more` / `next_cursor`
- reject or warn on scan-heavy filters and sorts

This must be explicit. The default DynamoDB backend should not silently behave
differently from SQLite/Postgres.

## Operations And Deployment

Configuration uses the existing storage target variable:

```bash
MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
```

Supported URL parameters include `region`, `endpoint_url`, `profile`, and
`create_table`.

Use synchronous `boto3` as an optional extra:

```bash
pip install 'morphdb[dynamodb]'
```

Credentials should come from AWS IAM and the boto3 credential chain, not from
secrets embedded in the MorphDB URL.

Local development and production use the same boto3 code path. Local DynamoDB or
LocalStack is selected with `endpoint_url`. Production should usually verify an
existing table; dev setup may opt into table creation with `create_table=true`.
When MorphDB creates a table, use on-demand billing by default.

## Consistency And Maintenance

Use DynamoDB transactions and conditional writes for normal multi-item writes so
object data, field indexes, and relations stay consistent. If an operation
exceeds DynamoDB transaction limits, fail clearly unless correctness can still be
guaranteed.

Indexed-field changes and reindexing are synchronous in the initial design:
correctness before low-latency schema edits. Large backfills/reindexes may be
expensive and can be revisited later with explicit background jobs and status
tracking.

## Scope

In scope:

- fresh DynamoDB deployments
- AWS deployment docs
- IAM policy examples
- dashboard logical views for DynamoDB
- optional DynamoDB Local/LocalStack integration tests

Out of scope for the initial backend:

- SQLite/Postgres-to-DynamoDB migration
- one table per app as the default
- async AWS SDK/runtime rewrite
- cursor pagination as the initial API
- production tuning for high-traffic SaaS workloads per app

Future options include one table per app for stronger isolation, cursor
pagination across all backends, cursor caching for page-number navigation, and
backend-specific performance docs that agents can read before generating app
code.
