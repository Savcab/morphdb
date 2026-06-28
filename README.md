# MorphDB

**A coding-agent-friendly, multi-tenant backend for vibe-coded websites.**

Reshape the data model as fast as your coding agent iterates — the frontend
keeps calling the same small set of generic, deterministic endpoints. One
process hosts many isolated apps (one per site). Zero dependencies on the
default SQLite engine; point it at PostgreSQL or DynamoDB when you want managed
persistence — same API, same code.

📖 **[Visual explainer → morphdb.pages.dev](https://morphdb.pages.dev)** — the whole idea (schema-fluid, API-stable), the agent/frontend split, relations, and how Claude plugs in through the `morphdb` CLI, on one page.

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

Data lives in `~/.morphdb/data.sqlite3` (change it with `--db PATH`,
`--db :memory:`, a Postgres `--db postgresql://...` URL, or a DynamoDB
`--db dynamodb://table?...` URL; move the state dir with `$MORPHDB_HOME`). Server flags:
`--host`, `--port`, `--db`. From a source checkout with no install, the
foreground server is `python3 -m morphdb --port 8787 --db ./app.sqlite3`.

To upgrade later: `pip install -U morphdb`, then `morphdb stop && morphdb start`
to reload the new code (data in `~/.morphdb` is preserved across `0.1.x`).

**Pointing clients at a hosted MorphDB.** Set `MORPHDB_HOST` to a full URL (e.g.
`https://db.example.com`) and the schema CLI — plus any frontend that reads
`window.MORPHDB_HOST` — calls that hosted server (running this same code) instead
of localhost. It's a client-side setting that names a *backend*, not a database
connection string.

### Persistence: SQLite, PostgreSQL, or DynamoDB

By default MorphDB is an embedded SQLite database — zero dependencies, one file.
To persist to **PostgreSQL** instead (a managed/networked database — RDS, Neon,
Supabase, or your own server), install the extra and point the server at a
connection URL:

```bash
pip install 'morphdb[postgres]'        # adds the psycopg driver

# pass a URL as --db …
morphdb start --db postgresql://user:pass@host:5432/mydb
# … or set it in the environment (handy for containers / serverless)
export MORPHDB_DATABASE_URL=postgresql://user:pass@host:5432/mydb
morphdb start
```

Nothing else changes — the same endpoints, schema model, queries, includes, CLI,
and dashboard work identically; the engine just talks to Postgres. This makes the
MorphDB process a **stateless API tier** you can run as a container (or several,
against one Postgres) with the durable state in your managed database. The core
stays zero-dependency; `psycopg` is pulled in only for the Postgres backend.

To persist to **DynamoDB**, install the DynamoDB extra and point MorphDB at one
DynamoDB table:

```bash
pip install 'morphdb[dynamodb]'        # adds boto3

# production: create/manage the table with IaC, then point MorphDB at it
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
morphdb start

# local/prototype: let MorphDB create the table on demand
morphdb start --db 'dynamodb://morphdb-dev?region=us-west-2&create_table=true'

# DynamoDB Local / LocalStack
morphdb start --db 'dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true'
```

DynamoDB credentials come from the normal boto3/AWS chain: environment variables,
profiles, web identity, EC2/ECS/Lambda roles, and so on. There is no database
password in the URL. MorphDB-created tables use on-demand billing and a fixed
single-table layout with two GSIs. For production, prefer creating the table and
IAM policy outside MorphDB and omit `create_table=true`.

DynamoDB keeps the same public MorphDB API, including exact `total`,
`limit`/`offset`, sorting, filters, relations, defaults, and includes. Some
patterns are correct but less efficient on DynamoDB than on SQLite/Postgres:
large offsets, exact totals on broad filtered reads, `contains`, negative
relation filters, and sorting/filter combinations that cannot map to one
DynamoDB access path. For prototype-sized apps this is usually fine; for larger
apps, prefer selective indexed filters, small offsets, and UI patterns like
"load more" over jumping directly to page 50.

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
  "fields": {"title":"string",
             "done":{"type":"boolean","index":true},
             "priority":{"type":"number","index":true}},
  "relations": {"assignee":{"to":"user","cardinality":"many_to_one","inverse":"tasks"}}}'

# 2. create + read + query
U=$(curl -s -X POST $BASE/objects/user -H "$H" -d '{"name":"Ann"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["_guid"])')
curl -X POST $BASE/objects/task -H "$H" -d "{\"title\":\"buy milk\",\"priority\":2,\"assignee\":\"$U\"}"
curl -H "$H" "$BASE/objects/task?done=false&sort=priority&order=desc"
curl -H "$H" "$BASE/objects/user/$U"          # → includes "tasks":[…]

# 3. morph the schema later — existing rows just gain the new field as null
curl -X PUT $BASE/schema/task -H "$H" -d '{"merge":true,"fields":{"due":"datetime"}}'
```

See [`examples/`](examples/) for a gallery of complete, single-file frontends backed
by MorphDB — a minimal todo plus LinkedIn-, Notion-, Figma-, and Linear-style
clones. Each ships the `morphdb.schema.json` that defines its data model, so you can
stand any of them up on your own MorphDB with `morphdb init` and run it unchanged. They
all hit the *same* generic endpoints — only the schema differs.

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
and `--db` (a SQLite path, `:memory:`, a `postgresql://...` URL, or a
`dynamodb://table?...` URL; default `$MORPHDB_DATABASE_URL` or
`~/.morphdb/data.sqlite3`).
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

### Editing the schema from the CLI (what the agent drives)

Beyond process control, the `morphdb` CLI edits an app's data model directly —
this is what the bundled Claude skill uses instead of hand-writing curl. Each
command takes the app key from `--app` or `$MORPHDB_APP` and talks to the running
server (or a hosted one via `$MORPHDB_HOST`):

```bash
morphdb app register my-site            # create a tenant (remember the key)
export MORPHDB_APP=my-site
morphdb schema add-field task title string
morphdb schema add-field task done boolean --default false --index   # --index → filterable/sortable
morphdb schema add-relation task assignee --to user --cardinality many_to_one --inverse tasks
morphdb schema list                     # all types; `schema show <type>` for one
morphdb query task 'done=false&sort=priority&limit=20'   # read-only peek, for debugging
```

Also: `schema drop-field`, `schema drop-relation`, `schema delete-type`, `schema
set <type> --json '{…}'` (raw-document escape hatch), and `app delete <key>`
(cascades). Reading/writing object *data* at runtime is the frontend's job — it
calls `/objects/...` over HTTP directly.

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

### Ship the schema with your repo (export / init)

A schema lives only inside the one MorphDB that holds it. Commit it to your
website's repo root as `morphdb.schema.json`, and anyone — a teammate, a fresh
deploy, someone who cloned your project onto their own MorphDB — stands the app
up with one idempotent command:

```bash
# author the app, then snapshot its data model to the repo root (commit this file)
morphdb export-schema my-site > morphdb.schema.json   # app key + every type's fields + relations
git add morphdb.schema.json && git commit -m "snapshot schema"

# on a clone / fresh MorphDB — the app key comes from inside the file
morphdb init                       # reads ./morphdb.schema.json by default
morphdb init path/to/schema.json   # or point at any export file
```

The export is self-contained, human-readable JSON. **Only the schema travels —
object data is not exported.** `init` is idempotent and safe to re-run:

- **App doesn't exist** → it's created and the schema applied.
- **App already exists** → the schema is **merged in additively; existing data is
  kept**. A name clash never deletes anything — re-running just converges the
  schema.
- **`morphdb init --reset`** → the only destructive path: delete the app and
  rebuild it clean from the file (prompts first when run interactively).

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
       (one process · many apps · SQLite or Postgres)
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

- **Zero dependencies by default.** Pure Python standard library + embedded SQLite (`python3 -m morphdb` and go). An optional PostgreSQL backend (`pip install morphdb[postgres]`) swaps in a networked, managed database with no other changes.
- **Generic CRUD** over arbitrary object types with typed fields.
- **Instant schema morphing** with lazy invalidation — O(1) regardless of data size.
- **Relations as fields** — four cardinalities, bidirectional, declared once, read/written on the object.
- **Query layer**: filter operators, sorting, pagination — all generic.
- **Multi-tenant by app** — one process backs many isolated sites; every call is scoped by an `X-App-Key`, and deleting an app cascades away all its data.
- **Wide-open CORS** so any frontend origin can call it in dev.
- **A management CLI** — `morphdb start/status/stop`, a read-only admin dashboard, and one-command skill install.
- **A Claude Code skill** (`morphdb/skill/SKILL.md`, install with `morphdb install-skill`) that drives the `morphdb schema`/`app`/`query` CLI so the agent edits the model without hand-writing curl.

> Scope: a small-scale developer tool. With the PostgreSQL backend it can run as
> one or more stateless instances behind a managed database, but it ships no
> multi-tenant auth or production durability guarantees.

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
On DynamoDB this exact parity may require reading and filtering more items
internally, especially for large offsets or broad filters.

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

A field is filterable/sortable only if its schema marks it **`"index": true`**
(opt-in, default off). Filtering or sorting an un-indexed field returns a 400
telling you to index it; turning the flag on backfills existing objects
automatically, turning it off is instant. `json` fields can't be indexed.

Append `__op` to an **indexed field** name: `eq` (default), `ne`, `gt`, `gte`,
`lt`, `lte`, `contains` (substring), `in` (comma-separated), `exists`
(`true`/`false`).

```
# priority, title, done, status all declared with "index": true
GET /objects/task?priority__gte=3&title__contains=buy&done=false
GET /objects/task?status__in=open,blocked&sort=priority&order=desc&limit=50
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

### Including related objects

By default a relation reads back as a guid (to-one) or list of guids (to-many).
Add `?include=` with comma-separated relation paths (dots nest) to hydrate them
into the full neighbor objects, nested Prisma-style:

```
GET /objects/post?include=author,comments,comments.author
# each post.author becomes a full user; post.comments a list of full comments,
# and each comment.author a full user too.
```

Works on the list endpoint and both single-object reads. Read-only, depth ≤ 4,
and batched (one query per relation per level — no N+1). Writes stay flat: create
and update with guids, never nested objects.

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
- **Pluggable persistence.** MorphDB exposes one logical storage interface;
  SQLite and PostgreSQL implement it with SQL tables, while DynamoDB implements
  it with native single-table items. The public schema/object API stays the same
  across backends, selected by the `--db` target. All access is serialized
  through a reentrant lock in the Python process; threaded request handling
  stays safe. Run several stateless instances against Postgres or DynamoDB when
  the durable state lives in managed storage.

## Limitations

- **Schema morphing is purely lazy.** Every schema edit — add, drop, or retype
  a field — rewrites only the one metadata row, never the stored objects (O(1)
  regardless of data size). After a **type change**, a value still stored at the
  old type simply reads as unset (the field's default, or null) until it's
  written again; reads and queries apply this rule identically, so they always
  agree. Re-adding a dropped field at the same type recovers its values.
- **Filtering/sorting is opt-in per field.** Only a field marked `"index": true`
  can be filtered or sorted (it gets a row in the indexed `field_index` table);
  an un-indexed field is storage-only and a filter/sort on it is a 400. Relations
  are always filterable via the indexed edge table — no flag needed.
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
- **DynamoDB performance parity.** The DynamoDB backend favors MorphDB API
  correctness over exposing a narrower, DynamoDB-specific API. Broad scans,
  large offsets, exact totals, `contains`, and some compound filters can be more
  expensive than their SQLite/Postgres equivalents. Future performance modes may
  opt out of exact totals or add cursor pagination.
- **Scale & auth.** With the default SQLite engine it's a localhost-scale tool;
  with the PostgreSQL or DynamoDB backends it can run as one or more stateless
  instances against managed storage. Either way it ships no built-in
  authentication or multi-tenant authorization.

## Development

```bash
python3 -m unittest discover -s tests   # full suite on SQLite, zero deps

# run the same engine suite against PostgreSQL too:
pip install 'morphdb[postgres,dev]'
MORPHDB_TEST_DATABASE_URL=postgresql://localhost/morphdb_test \
    python3 -m pytest tests/          # SQLite-specific tests auto-skip

# DynamoDB integration tests are intended for DynamoDB Local/LocalStack or AWS:
pip install 'morphdb[dynamodb,dev]'
MORPHDB_TEST_DATABASE_URL='dynamodb://morphdb-test?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true' \
    python3 -m pytest tests/
```

## License

MIT — see `LICENSE`.
