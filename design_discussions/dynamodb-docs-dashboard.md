# DynamoDB Docs And Dashboard

Date: 2026-06-28

Status: Accepted

## Decision

Add DynamoDB documentation for AWS deployment and support DynamoDB in the MorphDB
dashboard.

## AWS Documentation Direction

The AWS docs should add a DynamoDB-backed deployment path alongside the existing
Lambda + Postgres path.

Docs should cover:

- `MORPHDB_DATABASE_URL=dynamodb://...`
- required Lambda execution-role IAM permissions
- dev table creation with explicit `create_table=true`
- production recommendation to pre-provision and verify tables
- on-demand billing for MorphDB-created tables
- DynamoDB Local or LocalStack via `endpoint_url`
- performance caveats for exact totals, offset pagination, sorting, `contains`,
  negative filters, and broad scans
- existing warning that public Function URLs are unauthenticated unless an auth
  layer is added

## Dashboard Direction

The dashboard should work with DynamoDB through logical MorphDB views rather than
SQL table introspection.

Initial DynamoDB dashboard support can show:

- active backend and table target
- apps
- object schemas
- object counts where feasible
- relation schemas
- logical object browsing

SQL raw-table exploration can remain SQL-specific until a DynamoDB logical/raw
item explorer is designed.
