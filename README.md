# MorphDB

**A coding-agent-friendly, multi-tenant backend for vibe-coded websites.**

Reshape the data model as fast as your coding agent iterates — the frontend
keeps calling the same small set of generic, deterministic endpoints. One
process hosts many isolated apps (one per site), zero dependencies, backed by
SQLite.

## Install

```bash
pip install morphdb
```

Manage the local server with the `morphdb` CLI:

```bash
morphdb start          # run in the background (default 127.0.0.1:8787)
morphdb status         # running? where? how many apps?
morphdb stop           # stop it
morphdb run            # run in the foreground instead (blocking)
morphdb dashboard      # read-only web view of every app + its tables
morphdb install-skill  # install the MorphDB Claude Code skill (into ~/.claude)
```

Data lives in `~/.morphdb/data.sqlite3` (change it with `--db PATH` or
`--db :memory:`; move the state dir with `$MORPHDB_HOME`). Server flags:
`--host`, `--port`, `--db`. From a source checkout with no install, the
foreground server is `python3 -m morphdb --port 8787 --db ./app.sqlite3`.

To upgrade later: `pip install -U morphdb`, then `morphdb stop && morphdb start`
to reload the new code (data in `~/.morphdb` is preserved across `0.1.x`).

**Pointing clients at a hosted MorphDB.** Set `MORPHDB_HOST` to a full URL (e.g.
`https://db.example.com`) and the schema CLI — plus any frontend that reads
`window.MORPHDB_HOST` — calls that hosted server (running this same code) instead
of localhost. It's a client-side setting that names a *backend*, not a database
connection string.

## Use it

With the server running (`morphdb start`):

```bash
BASE=http://127.0.0.1:8787

# 0. register an app; send its key as X-App-Key on every schema/object call
curl -X POST $BASE/app -d '{"key":"my-site"}'
H="X-App-Key: my-site"

# 1. define types + a relation
curl -X PUT $BASE/schema/user -H "$H" -d '{"fields":{"name":"string"}}'
curl -X PUT $BASE/schema/task -H "$H" -d '{
  "fields": {"title":"string","done":"boolean","priority":"number"},
  "relations": {"assignee":{"to":"user","cardinality":"many_to_one","inverse":"tasks"}}}'

# 2. create + read + query
U=$(curl -s -X POST $BASE/objects/user -H "$H" -d '{"name":"Ann"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["_guid"])')
curl -X POST $BASE/objects/task -H "$H" -d "{\"title\":\"buy milk\",\"priority\":2,\"assignee\":\"$U\"}"
curl -H "$H" "$BASE/objects/task?done=false&sort=priority&order=desc"
curl -H "$H" "$BASE/objects/user/$U"          # → includes "tasks":[…]

# 3. morph the schema later — existing rows just gain the new field as null
curl -X PUT $BASE/schema/task -H "$H" -d '{"merge":true,"fields":{"due":"datetime"}}'
```

See `examples/todo/index.html` for a complete single-file frontend backed by MorphDB.

## Command-line interface

`morphdb` runs the server as a **background service** — `start` launches it
detached and hands your terminal straight back; `status` / `stop` find it again
via a pid file under the state dir.

| Command | What it does |
| --- | --- |
| `morphdb` or `morphdb start` | Start the server in the background (returns immediately). |
| `morphdb status` | Is it running? URL, pid, health, and app count. |
| `morphdb stop` | Stop the background server. |
| `morphdb logs` | Show the background server's log (`-n N` lines, `-f` to follow). |
| `morphdb run` | Run in the **foreground** (blocking) instead. |
| `morphdb dashboard` | Open a read-only web view of every app and its tables. |
| `morphdb install-skill` | Install the bundled Claude Code skill (below). |
| `morphdb --version` | Print the version. |

`start` / `run` accept `--host` (default `127.0.0.1`), `--port` (default `8787`),
and `--db` (a SQLite path or `:memory:`; default `~/.morphdb/data.sqlite3`).
`dashboard` accepts `--port` (default `8788`), `--db`, and `--no-open`. Service
state (pid, log, the default db) lives under `~/.morphdb` — relocate it with
`$MORPHDB_HOME`.

```bash
morphdb start                          # background, default 127.0.0.1:8787
morphdb start --port 9000 --db ./my.sqlite3
morphdb status                         # -> running (pid …) at http://… [healthy]
morphdb dashboard                      # opens http://127.0.0.1:8788
morphdb stop
morphdb run                            # foreground instead (Ctrl-C to quit)
```

### Install the Claude Code skill

`install-skill` writes the bundled MorphDB skill into a Claude skills directory,
so a coding agent automatically reaches for MorphDB when building a data-backed
site:

```bash
morphdb install-skill                  # -> ~/.claude/skills/morphdb (all projects)
morphdb install-skill --project        # -> ./.claude/skills/morphdb (current project)
morphdb install-skill --project DIR    # -> DIR/.claude/skills/morphdb
```

It installs the skill **bundled in the installed package** (not live from
GitHub) and is **idempotent** — re-running overwrites with the current version.
To get the newest skill, `pip install -U morphdb` first, then re-run. Restart
Claude Code afterward to pick it up.

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

```
   you (the coding agent)              the frontend you build
   ──────────────────────             ──────────────────────
   reshape the schema freely    │     calls fixed generic endpoints
   PUT    /schema/{type}        │     POST /objects/{type}
   GET    /schema               │     GET  /objects/{type}?field=…
   DELETE /schema/{type}        │     PATCH /objects/{type}/{guid}
            │                                    │
            └──────────────  MorphDB  ───────────┘
            (one process · many apps · SQLite)
                every call: X-App-Key: <app>
```

## The shape of it

One MorphDB process hosts **many apps** (one per website), fully isolated from
each other. Every schema and object request carries its app in the `X-App-Key`
header. There are three sets of endpoints:

- **App endpoints** — the tenant: `POST /app` to register a key you choose,
  `DELETE /app/{key}` to delete it and cascade away everything under it. There
  is no "list apps" — you only address an app whose key you already hold.
- **Schema endpoints** — the type model: `GET/PUT/DELETE /schema[/{type}]`.
  You, the agent, reshape these constantly (drive them with the schema CLI).
- **Object endpoints** — the data: `/objects/{type}` and `/object/{guid}`.
  Your frontend reads and writes here, and they never change as you morph the
  schema.

Within an app, type names are unique; the same name may be reused in another app.

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
- **Multi-tenant by app** — one process backs many isolated sites; every call is scoped by an `X-App-Key`, and deleting an app cascades away all its data.
- **Wide-open CORS** so any frontend origin can call it in dev.
- **A management CLI** — `morphdb start/status/stop`, a read-only admin dashboard, and one-command skill install.
- **A Claude Code skill** (`morphdb/skill/SKILL.md`, install with `morphdb install-skill`) with a schema CLI so the agent edits the model without hand-writing curl.

> Scope: a localhost-scale developer tool. Not built for multi-tenant auth,
> horizontal scale, or production durability guarantees.

## Data model

| Concept | What it is |
| --- | --- |
| **App** | A tenant: one website's isolated schema + data, addressed by a key sent in the `X-App-Key` header. |
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

Every schema and object request must send the app key as the `X-App-Key` header
(missing → `400`, unknown → `404`); the app endpoints below are the exception.

### App endpoints (one instance, many sites)

| Method & path | Body | Description |
| --- | --- | --- |
| `POST /app` | `{key}` | Register an app under a key you choose. `409` if taken. No list endpoint — remember the key. |
| `DELETE /app/{key}` | — | Delete an app and cascade-delete all its schemas, objects, relations, and edges. |

### Schema endpoints (you, the agent)

| Method & path | Body | Description |
| --- | --- | --- |
| `GET /schema` | — | All type schemas (fields + relations + inverse relations) for the app. |
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

Append `__op` to a **field** name: `eq` (default), `ne`, `gt`, `gte`, `lt`,
`lte`, `contains` (substring), `in` (comma-separated), `exists` (`true`/`false`).

```
GET /objects/task?priority__gte=3&title__contains=buy&done=false
GET /objects/task?status__in=open,blocked&sort=_created_at&order=desc&limit=50
```

You can also filter by a **relation** — treat it like an ORM foreign key, not a
manual join. Filtering by a relation matches objects linked to a given neighbor,
and resolves through the indexed edge table (so it is index-backed):

| Query | Meaning |
| --- | --- |
| `?assignee=<guid>` | objects whose `assignee` is / includes that neighbor |
| `?assignee__in=<g1>,<g2>` | linked to any of those neighbors |
| `?assignee__ne=<guid>` | not linked to that neighbor (includes unlinked) |
| `?assignee__exists=true` | has any `assignee` (`false` → has none) |

```
# "stages of business X that are still in build" — relation + field, one query
GET /objects/stage?business=<bizguid>&status=build&sort=_created_at
```

Relation filters compose with field filters, `sort`, and pagination. Scalar
comparisons (`gt`/`lt`/`contains`) are field-only; relations support
`eq`/`ne`/`in`/`exists`. So **model a foreign key as a relation, not a string
field** — you keep one-read traversal *and* get filtering, indexed, for free.

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
- **Apps are the tenant boundary.** Every row carries an `app` foreign key
  (`ON DELETE CASCADE`, with `PRAGMA foreign_keys=ON`); all reads and writes
  filter by it, so apps can reuse type names and never see each other's data,
  and deleting an app is a single cascading delete. Type identity is the
  `(app, name)` pair, and relation targets must live in the same app.
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
- **App keys are namespaces, not secrets.** The `X-App-Key` is an identifier in
  a plain header — it isolates data between apps but is **not** authentication.
  Anyone who knows a key can use that app; the absence of a list-apps endpoint is
  light obscurity, not a security boundary.
- Scope is a localhost-scale developer tool — no auth, no horizontal scale.

## Development

```bash
python3 -m unittest discover -s tests   # full suite, zero deps
```

## License

MIT — see `LICENSE`.
