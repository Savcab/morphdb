# DynamoDB Testing Strategy

Date: 2026-06-27

Status: Accepted

## Decision

DynamoDB testing should mirror the current Postgres testing approach.

The default test suite should remain zero-service and SQLite-based. DynamoDB
behavior tests should run only when an explicit DynamoDB test target is provided.

## Direction

- Keep default CI free of AWS and DynamoDB Local requirements.
- Support optional integration tests with `MORPHDB_TEST_DATABASE_URL`.
- Redirect `db.init_db(":memory:")` to a reset DynamoDB test table/prefix when
  the DynamoDB test target is configured.
- Run public behavior tests against DynamoDB.
- Skip SQL/SQLite-specific tests such as query-plan assertions and sqlite-file
  daemon management.
- Add DynamoDB-specific unit tests for target parsing, key layout, dependency
  errors, and query planning.

Example target:

```bash
MORPHDB_TEST_DATABASE_URL='dynamodb://morphdb-test?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true'
python -m pytest tests/
```

## Optional Tooling

DynamoDB Local can be used for production-like local integration tests. Moto may
be useful for fast unit tests, but should not be the only validation because
mocks can differ from real DynamoDB behavior.
