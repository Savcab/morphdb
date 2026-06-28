# DynamoDB Optional Dependency

Date: 2026-06-27

Status: Accepted

## Decision

Add DynamoDB support as an optional Python package extra.

Example:

```toml
[project.optional-dependencies]
dynamodb = ["boto3>=..."]
```

Users can install it with:

```bash
pip install 'morphdb[dynamodb]'
```

## Rationale

MorphDB's default SQLite backend currently has zero runtime dependencies. The
Postgres backend is already optional through `morphdb[postgres]`. DynamoDB should
follow the same pattern.

If a user configures a `dynamodb://` target without the optional dependency,
MorphDB should raise a clear install hint:

```text
DynamoDB support needs the boto3 driver. Install it with:
    pip install 'morphdb[dynamodb]'
```
