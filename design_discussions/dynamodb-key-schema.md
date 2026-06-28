# DynamoDB Key Schema

Date: 2026-06-28

Status: Accepted

## Decision

Use one DynamoDB table with string primary keys:

```text
pk  (S)
sk  (S)
```

Use a small fixed set of optional secondary indexes for admin and common system
sorts, but make MorphDB's dynamic field and relation indexes explicit item
collections in the base table. Do not create one GSI per MorphDB field.

This keeps field/relation query items strongly queryable through the base table
and avoids relying on eventually consistent GSIs for core MorphDB correctness.

## Fixed GSIs

Create these fixed GSIs when MorphDB creates the table:

```text
by_app
  gsi1pk (S)
  gsi1sk (S)
  projection: KEYS_ONLY

by_type_updated
  gsi2pk (S)
  gsi2sk (S)
  projection: ALL or INCLUDE guid/source metadata
```

`by_app` is for admin, cleanup, dashboard, and repair tooling. It should not be
the only correctness path for normal object reads.

`by_type_updated` supports efficient `_updated_at` list ordering for object refs.
If it proves unnecessary in PR2, it may be deferred, but it is the intended fixed
index for that access path.

## Encoding Rules

All key segments should be escaped or encoded so user-controlled app keys, type
names, field names, relation names, and guids cannot collide with separators.

Use explicit type prefixes in keys:

```text
APP#
TYPE#
GUID#
OBJ#
FIELD#
ASSOC#
REL#
EDGE#
VAL#
```

Use sortable timestamp strings for `created_at` and `updated_at`; MorphDB already
normalizes datetimes to ISO-like strings that sort lexicographically.

For field-index values, define a deterministic `encode_index_value(type, value)`
helper. It must preserve ordering for strings, datetimes, booleans, and numbers
well enough for MorphDB's comparison/sort semantics. If an edge case cannot be
encoded safely, the planner must fall back to application-side comparison for
correctness.

## App Items

App existence:

```text
pk = APP#{app}
sk = META
kind = app
created_at = ...
gsi1pk = APP#{app}
gsi1sk = META
```

App registry for dashboard/listing:

```text
pk = APPS
sk = APP#{app}
kind = app_ref
created_at = ...
```

## Schema Items

Object schema:

```text
pk = APP#{app}#SCHEMA
sk = TYPE#{type}
kind = object_schema
fields = <json>
created_at = ...
updated_at = ...
gsi1pk = APP#{app}
gsi1sk = SCHEMA#TYPE#{type}
```

Association schema:

```text
pk = APP#{app}#ASSOC_SCHEMA
sk = ASSOC#{assoc_name}
kind = association_schema
from_type = ...
to_type = ...
forward_name = ...
inverse_name = ...
cardinality = ...
symmetric = ...
created_at = ...
updated_at = ...
gsi1pk = APP#{app}
gsi1sk = ASSOC_SCHEMA#ASSOC#{assoc_name}
```

Relation views can query all association schemas for an app and filter in
application code. The number of relation schemas per prototype app is expected to
be modest.

## Object Items

Object source item, sharded by guid so a busy app is not forced into one object
partition:

```text
pk = APP#{app}#OBJ#{shard(guid)}
sk = GUID#{guid}
kind = object
app = ...
guid = ...
object_type = ...
data = <json>
created_at = ...
updated_at = ...
gsi1pk = APP#{app}
gsi1sk = OBJ#TYPE#{type}#GUID#{guid}
```

Object list ref, ordered by created time for the default list path:

```text
pk = APP#{app}#TYPE#{type}
sk = OBJ#C#{created_at}#G#{guid}
kind = object_ref
guid = ...
object_type = ...
source_pk = APP#{app}#OBJ#{shard(guid)}
source_sk = GUID#{guid}
created_at = ...
updated_at = ...
gsi1pk = APP#{app}
gsi1sk = OBJ_REF#TYPE#{type}#GUID#{guid}
gsi2pk = APP#{app}#TYPE#{type}
gsi2sk = OBJ#U#{updated_at}#G#{guid}
```

Guid owner item, used to preserve MorphDB's global-guid uniqueness semantics for
caller-supplied upserts:

```text
pk = GUID#{guid}
sk = OWNER
kind = guid_owner
app = ...
object_type = ...
created_at = ...
```

Reads by app+guid use the object source item directly. Reads by type list use the
object list refs, then batch-get source items.

## Field Index Items

Field-index items are derived records. Object items remain the source of truth.

For each stored, type-valid indexed scalar field value:

```text
pk = FIDX#APP#{app}#TYPE#{type}#FIELD#{field}#VT#{value_type}
sk = VAL#{encoded_value}#G#{guid}
kind = field_index
app = ...
object_type = ...
field_name = ...
guid = ...
value_type = ...
value = <typed value>
created_at = <object created_at>
updated_at = <object updated_at>
gsi1pk = APP#{app}
gsi1sk = FIDX#TYPE#{type}#FIELD#{field}#GUID#{guid}
```

Access patterns:

- equality: query partition and `begins_with(sk, VAL#{encoded_value}#)`
- range: query partition with sort-key comparisons
- exists true: query the field partition
- exists false: base object set minus field-index matches
- sort by indexed field: query the field partition in value order when possible

Missing fields and stale/wrong-type values do not get field-index items. The
query layer must still account for lazy defaults, as SQLite/Postgres do today.

## Relation Items

Canonical edge item, unique per edge:

```text
pk = EDGE#APP#{app}#ASSOC#{assoc_name}
sk = FROM#{from_guid}#TO#{to_guid}
kind = edge
app = ...
assoc_name = ...
from_guid = ...
to_guid = ...
created_at = ...
gsi1pk = APP#{app}
gsi1sk = EDGE#ASSOC#{assoc_name}#FROM#{from_guid}#TO#{to_guid}
```

Object adjacency item, for projecting relations onto an object:

```text
pk = REL#APP#{app}#OBJ#{object_guid}
sk = ASSOC#{assoc_name}#SIDE#{side}#C#{created_at}#NB#{neighbor_guid}
kind = relation_object
app = ...
assoc_name = ...
side = from|to|sym
object_guid = ...
neighbor_guid = ...
from_guid = ...
to_guid = ...
created_at = ...
gsi1pk = APP#{app}
gsi1sk = REL_OBJ#OBJ#{object_guid}#ASSOC#{assoc_name}#NB#{neighbor_guid}
```

Neighbor index item, for positive relation filters:

```text
pk = RIDX#APP#{app}#ASSOC#{assoc_name}#SIDE#{side}#NB#{neighbor_guid}
sk = OBJ#{object_guid}#C#{created_at}
kind = relation_neighbor_index
app = ...
assoc_name = ...
side = from|to|sym
object_guid = ...
neighbor_guid = ...
from_guid = ...
to_guid = ...
created_at = ...
gsi1pk = APP#{app}
gsi1sk = RIDX#ASSOC#{assoc_name}#OBJ#{object_guid}#NB#{neighbor_guid}
```

Relation slot/existence item, for `exists=true` and cardinality checks:

```text
pk = RSLOT#APP#{app}#ASSOC#{assoc_name}#SIDE#{side}
sk = OBJ#{object_guid}
kind = relation_slot
app = ...
assoc_name = ...
side = from|to|sym
object_guid = ...
count = <number of edges visible from this side>
updated_at = ...
gsi1pk = APP#{app}
gsi1sk = RSLOT#ASSOC#{assoc_name}#SIDE#{side}#OBJ#{object_guid}
```

For symmetric relations, store the canonical edge once, but store object
adjacency and neighbor-index records for both object perspectives.

Access patterns:

- project relations for an object: query `REL#APP#{app}#OBJ#{guid}`
- relation equality: query `RIDX#...#NB#{neighbor_guid}`
- relation `in`: run multiple neighbor-index queries and union/intersect
- `exists=true`: query `RSLOT#...`
- `ne` and `exists=false`: base object set minus relation matches

## Write Rules

Object create transaction:

- conditionally put guid owner
- put object source
- put object list ref
- put field-index items
- put relation items if relation values are present

Object update transaction:

- get existing source object
- update source object
- update list ref metadata
- delete old field-index items computed from the old blob/schema
- put new field-index items computed from the new blob/schema
- apply relation set-as-field updates

Object delete:

- get source object
- delete source object
- delete object list ref
- delete guid owner
- delete field-index items computed from the source blob/schema
- query object adjacency and delete all edge/index/slot records touching the
  object

Schema/index backfill and reindex operations may use multiple internal batches,
but they remain logically synchronous in the initial backend.

## Query Planner Rules

The DynamoDB list planner should pick a base candidate path:

1. positive relation equality/in filters through `RIDX`
2. indexed scalar equality/range filters through `FIDX`
3. app/type object listing through object refs

Then apply remaining filters, lazy default logic, exact total calculation,
sorting, offset, and limit for MorphDB parity.

Efficient paths should be used when they are available. If a query cannot be
served efficiently by the key schema, the backend should still return the correct
MorphDB result and rely on backend performance docs to explain the cost.

## DynamoDB Limits To Respect

Implementation must chunk and retry around DynamoDB API limits, including:

- `Query` returns up to 1 MB before pagination.
- `BatchGetItem` retrieves up to 100 items per call.
- `BatchWriteItem` writes/deletes up to 25 items per call and requires retrying
  unprocessed items with backoff.
- `TransactWriteItems` groups up to 100 actions and 4 MB per transaction.
- Individual items are limited to 400 KB.
- GSI reads are eventually consistent, so core correctness paths should prefer
  base-table item collections where practical.
