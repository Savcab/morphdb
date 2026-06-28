# DynamoDB Test Tooling

Date: 2026-06-28

Status: Accepted

## Decision

Use DynamoDB Local or LocalStack as the primary optional integration-test target
for the DynamoDB backend.

Real AWS tests may be supported as optional/manual validation. Moto may be used
only as a supplementary helper for narrow unit tests if useful.

## Test Layers

- Default suite: SQLite only, no AWS and no local DynamoDB dependency.
- Unit tests: target parsing, optional dependency errors, key encoding, query
  planning, and layout helpers.
- Optional integration tests: DynamoDB Local or LocalStack through `endpoint_url`.
- Optional manual/release tests: real AWS DynamoDB table.

## Rationale

DynamoDB Local/LocalStack exercise the boto3 DynamoDB code path more realistically
than pure mocks while avoiding AWS credentials and cost in the default suite.

Moto is fast and useful for some AWS-shaped unit tests, but it should not be the
only validation because mocks can differ from real DynamoDB semantics.
