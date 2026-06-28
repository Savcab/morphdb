# DynamoDB Environment Configuration

Date: 2026-06-27

Status: Accepted

## Decision

Use `MORPHDB_DATABASE_URL` for DynamoDB targets.

Example:

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
```

Do not add required DynamoDB-specific environment variables such as
`MORPHDB_DYNAMODB_TABLE` or `MORPHDB_DYNAMODB_REGION`.

## Rationale

`MORPHDB_DATABASE_URL` is already MorphDB's storage target configuration. Keeping
DynamoDB under the same setting preserves the existing backend selection model
used by SQLite paths and Postgres URLs.
