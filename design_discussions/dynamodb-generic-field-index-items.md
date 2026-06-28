# DynamoDB Generic Field Index Items

Date: 2026-06-28

Status: Accepted

## Decision

Use generic derived field-index items in DynamoDB, equivalent in purpose to the
SQL `field_index` table.

Object items remain the source of truth. Field-index items are derived and can be
rebuilt from object blobs plus schema definitions.

## Purpose

Generic field-index items let MorphDB support dynamic app-defined fields without
creating a DynamoDB GSI or table-level infrastructure change per field.

Example object data:

```json
{
  "_guid": "task_123",
  "status": "todo",
  "priority": 3
}
```

Conceptual derived index items:

```text
FIELD app=demo type=task field=status   value=todo object=task_123
FIELD app=demo type=task field=priority value=3    object=task_123
```

A query like:

```http
GET /objects/task?status=todo
```

can read field-index items for `status=todo`, collect matching object IDs, and
then fetch the source object items.

## Benefits

- avoids scanning every object for common indexed filters
- avoids one GSI per MorphDB field
- keeps schema edits lightweight
- allows field indexes to be repaired by reindexing from source objects
- mirrors the existing SQLite/Postgres `field_index` design
