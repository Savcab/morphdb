# DynamoDB Default Values

Date: 2026-06-28

Status: Accepted

## Decision

Preserve MorphDB's existing lazy default-value semantics for the initial DynamoDB
backend.

Defaults should not be materialized into every object item.

## Current MorphDB Semantics

Objects store their actual data blob. Reads project missing fields through the
current object schema.

For example, if a schema defines:

```json
{
  "done": { "type": "boolean", "default": false, "index": true }
}
```

an object that does not store `done` still reads as:

```json
{ "done": false }
```

## DynamoDB Direction

The DynamoDB backend should follow the same model:

- object items store only actual field data
- schema projection applies defaults on read
- field-index records represent actual stored, type-valid values only
- missing or stale values are interpreted through the current schema default
  during filtering and sorting
- filters/sorts that match defaults may need base-object checks to preserve exact
  MorphDB behavior

## Rationale

Lazy defaults are central to MorphDB's schema-fluid design:

- adding a field with a default does not rewrite objects
- changing a default does not rewrite objects
- retyping a field does not rewrite blobs
- schema edits stay lightweight

Materializing defaults into DynamoDB object items would make some queries simpler,
but it would turn schema changes into bulk data migrations and diverge from the
current SQLite/Postgres behavior.
