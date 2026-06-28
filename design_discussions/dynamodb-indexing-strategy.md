# DynamoDB Indexing Strategy

Date: 2026-06-27

Status: Accepted

## Decision

Use a small fixed set of DynamoDB GSIs plus generic MorphDB index items. Do not
create one DynamoDB GSI per user-defined MorphDB field.

## Direction

When a MorphDB schema marks a field as indexed:

```json
{
  "status": { "type": "string", "index": true }
}
```

the DynamoDB backend should maintain generic field-index records, not create AWS
infrastructure for `status`.

This preserves MorphDB's schema-fluid model:

- schema edits stay lightweight
- no per-field AWS migrations
- one table/index layout can support many apps and many dynamic schemas

## Candidate Access Paths

Fixed table/index layout should support:

- object lookup by guid
- object listing by app/type
- field index queries
- relation index queries
- batch object fetch after index/relation lookup

The exact PK/SK and GSI names can be refined during implementation, but the
principle is fixed GSIs plus generic index records.
