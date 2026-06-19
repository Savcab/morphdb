# MorphDB

**A schema-fluid, API-stable database for AI-generated apps.**

Reshape the data model as fast as your coding agent iterates — the frontend
keeps calling the same small set of generic, deterministic endpoints.

```
   you (the coding agent)              the frontend you build
   ──────────────────────             ──────────────────────
   reshape the schema freely    │     calls fixed generic endpoints
   PUT    /schema/{type}        │     POST /objects/{type}
   GET    /schema               │     GET  /objects/{type}?field=…
   DELETE /schema/{type}        │     PATCH /objects/{type}/{guid}
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

## The shape of it

There are just **two sets of endpoints**:

- **Schema endpoints** — the type model: `GET/PUT/DELETE /schema[/{type}]`.
  You, the agent, reshape these constantly (drive them with the schema CLI).
- **Object endpoints** — the data: `/objects/{type}` and `/object/{guid}`.
  Your frontend reads and writes here, and they never change as you morph the
  schema.

A **type** is one document with `fields` (raw values) and `relations` (links to
other types). Relations are declared once but read and written **like ordinary
fields** on the object body — so the frontend never learns a separate
"associations" API.

```jsonc
// PUT /schema/task
{
  "fields": {
    "title": "string",
    "done":  { "type": "boolean", "default": false }
  },
  "relations": {
    // declared once on `task`; `user.tasks` appears automatically
    "assignee": { "to": "user", "cardinality": "many_to_one", "inverse": "tasks" }
  }
}
```

```jsonc
// GET /objects/task/<guid>  → relations are right there, as guids
{ "_guid": "task_…", "_type": "task", "title": "ship", "done": false,
  "assignee": "user_…" }

// GET /objects/user/<guid>  → the inverse side, automatically
{ "_guid": "user_…", "_type": "user", "name": "Ann",
  "tasks": ["task_…", "task_…"] }
```

```bash
# link them by writing the relation like a field
curl -X PATCH $BASE/objects/task/<t> -d '{"assignee":"<u>"}'
# to-many is a list; null or [] clears
curl -X PATCH $BASE/objects/user/<u> -d '{"tasks":["<t1>","<t2>"]}'
```

## Features

- **Zero dependencies.** Pure Python standard library + SQLite. `python3 -m morphdb` and go.
- **Generic CRUD** over arbitrary object types with typed fields.
- **Instant schema morphing** with lazy invalidation — O(1) regardless of data size.
- **Relations as fields** — four cardinalities, bidirectional, declared once, read/written on the object.
- **Query layer**: filter operators, sorting, pagination — all generic.
- **Wide-open CORS** so any frontend origin can call it in dev.
- **A Claude Code skill** (`skill/SKILL.md`) with a schema CLI so the agent edits the model without hand-writing curl.

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

# 1. define types + a relation
curl -X PUT $BASE/schema/user -d '{"fields":{"name":"string"}}'
curl -X PUT $BASE/schema/task -d '{
  "fields": {"title":"string","done":"boolean","priority":"number"},
  "relations": {"assignee":{"to":"user","cardinality":"many_to_one","inverse":"tasks"}}}'

# 2. create + read + query
U=$(curl -s -X POST $BASE/objects/user -d '{"name":"Ann"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["_guid"])')
curl -X POST $BASE/objects/task -d "{\"title\":\"buy milk\",\"priority\":2,\"assignee\":\"$U\"}"
curl "$BASE/objects/task?done=false&sort=priority&order=desc"
curl "$BASE/objects/user/$U"          # → includes "tasks":[…]

# 3. morph the schema later — existing rows just gain the new field as null
curl -X PUT $BASE/schema/task -d '{"merge":true,"fields":{"due":"datetime"}}'
```

See `examples/todo/index.html` for a complete single-file frontend backed by MorphDB.

## Data model

| Concept | What it is |
| --- | --- |
| **Type** | A named schema: `fields` (raw values) + `relations` (links). The thing you morph. |
| **Object** | An instance: a `_guid`, a type, field values (JSON blob) + relation guids (edges). |
| **Relation** | A typed link with a cardinality, declared on one type, visible (as the inverse) on both. |
| **Edge** | One link between two object guids. Stored once; traversable from both ends. |

**Field types:** `string`, `number`, `boolean`, `json`, `datetime`.
Values are coerced to the declared type on write; unknown fields/relations are
rejected. `number` rejects NaN/Infinity; `datetime` is validated as ISO-8601
(or epoch seconds) and normalized. Field defaults are materialized into storage
on write, so a defaulted value is queryable like any other.

**System fields** on every object: `_guid`, `_type`, `_created_at`,
`_updated_at`. Field and relation names may not begin with `_`, and a relation
may not share a name with a field on the same type.

**Relations.** Declared inside a type under `relations`:

```jsonc
"assignee": {
  "to": "user",                 // neighbor type
  "cardinality": "many_to_one", // many tasks → one user
  "inverse": "tasks",           // the name the user side sees
  "description": "…",           // optional
  "inverse_description": "…"    // optional
}
```

Cardinality `X_to_Y` means the **from** side sees `Y` neighbors and the **to**
side sees `X`. So `many_to_one` gives `task.assignee` a single guid and
`user.tasks` a list. Reading an object includes all its relations (both
directions); writing a relation key sets that relation's full set
(set-as-field), with **last-write-wins** if a single-valued slot is already
taken. `null`/`[]` clears.

**Symmetric relations.** For a mutual relationship within one type (friends,
peers), set `symmetric: true` (requires `to` == the declaring type and a
cardinality of `one_to_one` or `many_to_many`). The edge A–B and B–A are then
the same edge — created idempotently in either order, counted once, traversed
from both ends under one shared label.

**List responses** are shaped `{"objects": [...], "total": <full filtered
count>, "limit": <int>, "offset": <int>}` — `total` is the count across the
whole filter, not just the returned page. Default `limit` is 100 (max 1000).

## API reference

### Schema endpoints (you, the agent)

| Method & path | Body | Description |
| --- | --- | --- |
| `GET /schema` | — | All type schemas (fields + relations + inverse relations). |
| `GET /schema/{type}` | — | One type's schema. |
| `PUT /schema/{type}` | `{fields?, relations?, merge?}` or a bare field map | Create/replace a type. `merge:true` adds without dropping. Absent `fields`/`relations` are left untouched. |
| `DELETE /schema/{type}` | — | Delete a type, its objects, and edges touching them. Neighbor objects survive. |

### Object endpoints (your frontend)

| Method & path | Body / query | Description |
| --- | --- | --- |
| `POST /objects/{type}` | field + relation values | Create an object → returns it with `_guid`. |
| `GET /objects/{type}` | filters, `limit`, `offset`, `sort`, `order` | List / query. |
| `GET /objects/{type}/{guid}` | — | Read one (type-checked). |
| `GET /object/{guid}` | — | Read one by guid alone. |
| `PUT /objects/{type}/{guid}` | field + relation values | Replace fields (create if absent); set any relations present. |
| `PATCH /objects/{type}/{guid}` | partial fields + relations | Merge fields (create if absent); set any relations present. |
| `DELETE /objects/{type}/{guid}` | — | Delete object + its edges. |

### Query operators

Append `__op` to a field name: `eq` (default), `ne`, `gt`, `gte`, `lt`, `lte`,
`contains` (substring), `in` (comma-separated), `exists` (`true`/`false`).
Filtering is on **fields**, not relations.

```
GET /objects/task?priority__gte=3&title__contains=buy&done=false
GET /objects/task?status__in=open,blocked&sort=_created_at&order=desc&limit=50
```

## Errors

JSON shape: `{"error": {"code": "...", "message": "...", ...extra}}`.
Status codes: `400` bad request/validation, `404` not found, `405` method not
allowed, `413` body too large, `500` internal.

## Design notes

- **Lazy invalidation.** Objects are stored as JSON blobs and projected through
  the live schema on every read. Schema edits never touch stored rows, so they
  are constant-time. A dropped field's data lingers in the blob (hidden) and
  reappears if the field is re-added at the same type.
- **Relations are fields, edges are rows.** A relation is exposed as a field on
  the object body but stored as a single canonical row per edge. Bidirectional
  traversal queries both endpoint columns (both indexed). This avoids the
  dual-write hazard of mirrored rows while letting an object surface all of its
  links in one read.
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
- **Filtering is field-only.** Query operators apply to raw fields; relations
  are read/written on the object body but not filtered server-side (yet).
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
