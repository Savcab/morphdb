# MorphDB

**A schema-fluid, API-stable database for AI-generated apps.**

Reshape the data model as fast as your coding agent iterates — the frontend
keeps calling the same small set of generic, deterministic endpoints.

```
   you (the coding agent)              the frontend you build
   ──────────────────────             ──────────────────────
   reshape the schema freely    │     calls fixed generic endpoints
   POST /schemas/objects        │     POST /objects/{type}
   PUT  /schemas/objects/{t}    │     GET  /objects/{type}?field=…
   POST /schemas/associations   │     POST /associations
            │                                    │
            └──────────────  MorphDB  ───────────┘
                       (one process, SQLite)
```

## Why

AI coding agents are great at building HTML/CSS/JS frontends but thrash hard on
backends: every UI iteration wants a slightly different data shape, and most
databases make schema change painful (migrations, downtime, rewriting rows). So
vibe-coded apps stay frontend-only and lose their data on refresh.

MorphDB removes the friction. The schema is just metadata; objects are JSON
blobs reinterpreted through the **current** schema on every read (lazy
invalidation). Adding, removing, or retyping a field is an O(1) metadata edit —
**no migration, no row rewrite, no downtime** — regardless of how much data
exists. Meanwhile the frontend talks to generic endpoints that never change.

## Features

- **Zero dependencies.** Pure Python standard library + SQLite. `python3 -m morphdb` and go.
- **Generic CRUD** over arbitrary object types with typed fields.
- **Instant schema morphing** with lazy invalidation — dropped fields are hidden, not destroyed (re-add to recover).
- **Relationships** with four cardinalities and named bidirectional traversal.
- **Query layer**: filter operators, sorting, pagination — all generic.
- **Wide-open CORS** so any frontend origin can call it in dev.
- **A Claude Code skill** (`skill/SKILL.md`) that teaches an agent to drive it.

> Scope: a localhost-scale developer tool. Not built for multi-tenant auth,
> horizontal scale, or production durability guarantees.

## Install / run

No install required:

```bash
python3 -m morphdb --port 8787 --db ./app.sqlite3
```

Or install the console script:

```bash
pip install -e .
morphdb --port 8787 --db ./app.sqlite3
```

Flags: `--host` (default `127.0.0.1`), `--port` (default `8787`),
`--db` (default `morphdb.sqlite3`; use `:memory:` for ephemeral).

Then: `curl http://127.0.0.1:8787/help` for a live reference.

## Quickstart

```bash
BASE=http://127.0.0.1:8787

# 1. define a type
curl -X POST $BASE/schemas/objects -d '{
  "name":"task","fields":{"title":"string","done":"boolean","priority":"number"}}'

# 2. create + read + query
curl -X POST $BASE/objects/task -d '{"title":"buy milk","done":false,"priority":2}'
curl "$BASE/objects/task?done=false&sort=priority&order=desc"

# 3. morph the schema later — existing rows just gain the new field as null
curl -X PUT $BASE/schemas/objects/task -d '{"merge":true,"fields":{"due":"datetime"}}'
```

See `examples/todo/index.html` for a complete single-file frontend backed by MorphDB.

## Data model

| Concept | What it is |
| --- | --- |
| **Object schema** | A named type with typed fields. The thing you morph. |
| **Object** | An instance: a `_guid`, a type, and field values (stored as a JSON blob). |
| **Association schema** | A named relationship: from-type, to-type, a label per direction, a cardinality. |
| **Association** | An edge between two object guids. Stored once; traversable from both ends. |

**Field types:** `string`, `number`, `boolean`, `json`, `datetime`.
Values are coerced to the declared type on write; unknown fields are rejected.
`number` rejects NaN/Infinity; `datetime` is validated as ISO-8601 (or epoch
seconds) and normalized. Field defaults are materialized into storage on write,
so a defaulted value is queryable like any other.

**System fields** on every object: `_guid`, `_type`, `_created_at`,
`_updated_at`. Schema field names may not begin with `_`.

**Cardinalities:** `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`,
enforced on edge creation (409 on conflict; `?replace=true` to override).

**Symmetric associations.** For a mutual relationship within one type (friends,
peers), set `symmetric: true` (requires `from_type == to_type` and a cardinality
of `one_to_one` or `many_to_many`). The edge A–B and B–A are then the same edge:
created idempotently in either order, counted once, and traversed from both
ends under a single `relation` label.

**List responses** are shaped `{"objects": [...], "total": <full filtered
count>, "limit": <int>, "offset": <int>}` — `total` is the count across the
whole filter, not just the returned page. Default `limit` is 100 (max 1000).

## API reference

### Schema management (the coding agent)

| Method & path | Body / query | Description |
| --- | --- | --- |
| `GET /schema` | — | View all object + association schemas. |
| `GET /schemas/objects` | — | List object schemas. |
| `GET /schemas/objects/{type}` | — | View one object schema. |
| `POST /schemas/objects` | `{name, fields, merge?}` | Create/replace an object type. |
| `PUT /schemas/objects/{type}` | `{fields, merge?}` or raw fields map | Create/replace a type. `merge:true` adds fields without restating the rest. |
| `DELETE /schemas/objects/{type}` | `?cascade=true` (default) | Delete a type; cascades to its objects + edges. |
| `POST /schemas/objects/{type}/delete-fields` | `{fields:[...]}` | Remove fields (data hidden, not destroyed). |
| `GET /schemas/associations` | — | List association types. |
| `GET /schemas/associations/{name}` | — | View one association type. |
| `POST /schemas/associations` | `{name, from_type, to_type, forward_name, inverse_name, cardinality, symmetric?}` | Create/replace a relationship type. |
| `PUT /schemas/associations/{name}` | same fields | Create/replace by name. |
| `DELETE /schemas/associations/{name}` | `?cascade=true` (default) | Delete a relationship type + its edges. |

### Data (the frontend)

| Method & path | Body / query | Description |
| --- | --- | --- |
| `POST /objects/{type}` | field values | Create an object → returns it with `_guid`. |
| `GET /objects/{type}` | filters, `limit`, `offset`, `sort`, `order` | List / query. |
| `GET /objects/{type}/{guid}` | — | Read one (type-checked). |
| `GET /object/{guid}` | — | Read one by guid alone. |
| `PUT /objects/{type}/{guid}` | field values | Replace data (create if absent). |
| `PATCH /objects/{type}/{guid}` | partial fields | Merge fields (create if absent). |
| `DELETE /objects/{type}/{guid}` | — | Delete object + its edges. |
| `GET /object/{guid}/associations` | `name`, `relation`, `direction`, `expand` | List edges from this object's perspective. |
| `POST /associations` | `{assoc_name, from_guid, to_guid}`, `?replace` | Create an edge. |
| `DELETE /associations` | `{assoc_name, from_guid, to_guid}` | Delete an edge. |

### Query operators

Append `__op` to a field name: `eq` (default), `ne`, `gt`, `gte`, `lt`, `lte`,
`contains` (substring), `in` (comma-separated), `exists` (`true`/`false`).

```
GET /objects/task?priority__gte=3&title__contains=buy&done=false
GET /objects/task?status__in=open,blocked&sort=_created_at&order=desc&limit=50
```

### Association traversal

Edges are stored once but traversable from both ends. From an object's point of
view each edge reports the neighbor's `relation` (the label for that direction),
`direction` (`forward`/`inverse`), `neighbor_guid`, and `neighbor_type`. Add
`expand=true` to inline the neighbor object.

```
GET /object/<user>/associations?relation=tasks&expand=true   # a user's tasks
GET /object/<task>/associations?relation=assignee            # a task's owner
```

## Errors

JSON shape: `{"error": {"code": "...", "message": "...", ...extra}}`.
Status codes: `400` bad request/validation, `404` not found, `405` method not
allowed, `409` cardinality conflict, `413` body too large, `500` internal.

## Design notes

- **Lazy invalidation.** Objects are stored as JSON blobs and projected through
  the live schema on every read. Schema edits never touch stored rows, so they
  are constant-time. A dropped field's data lingers in the blob (hidden) and
  reappears if the field is re-added.
- **Single-row associations.** Each edge is one row; bidirectional traversal
  queries both endpoint columns (both indexed). This avoids the dual-write
  consistency hazard of storing mirrored rows.
- **One connection, one lock.** All access is serialized through a single
  SQLite connection guarded by a reentrant lock — simple and correct at
  localhost scale; threaded request handling stays safe.

## Limitations

- **Schema morphing is purely lazy.** Every schema edit — add, drop, or retype
  a field — rewrites only the one metadata row, never the stored objects (O(1)
  regardless of data size). After a **type change**, a value still stored at the
  old type simply reads as unset (the field's default, or null) until it's
  written again; reads and queries apply this rule identically, so they always
  agree. Re-adding a dropped field at the same type recovers its values.
- **Integer magnitude.** Numbers are stored and read back exactly at any size.
  Filtering/sorting on integers beyond ±2⁶³ uses floating-point comparison (a
  SQLite limitation), so equality/range queries on such huge integers may be
  imprecise even though reads are exact.
- **HTTP verbs.** Only `GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD` are part of the
  API; other verbs (e.g. `TRACE`) get the stdlib's plain `501`.
- Scope is a localhost-scale developer tool — no auth, no horizontal scale.

## Development

```bash
python3 -m unittest discover -s tests   # full suite, zero deps
```

## License

MIT — see `LICENSE`.
