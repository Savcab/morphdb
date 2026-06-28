# DynamoDB Local And Production Behavior

Date: 2026-06-27

Status: Accepted

## Decision

Use the same synchronous boto3 DynamoDB backend code path for local development
and production.

The only difference should be endpoint configuration.

## Local Example

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true'
```

This can point at DynamoDB Local or LocalStack.

## Production Example

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
```

Production omits `endpoint_url`, so boto3 talks to AWS DynamoDB.

## Rationale

Using one code path keeps local behavior close to production behavior. There
should not be a separate fake in-memory DynamoDB backend.
