---
name: morphdb
description: Spin up an instant, schema-fluid backend for a vibe-coded app. Use when you are building an HTML/CSS/JS frontend that needs to persist and query data (todos, CRM, dashboard, tracker, any CRUD app) but you do not want to hand-write and re-migrate a database schema as the design churns. MorphDB gives you generic, stable REST endpoints; you reshape the schema with one call whenever the app's needs change, and the frontend never has to change which endpoints it calls. Trigger when the user asks for a "backend for this", "make it save data", "add a database", or "persist this" for a small/local web app.
---

# MorphDB — instant morphable backend for AI-built apps

MorphDB is a single Python process (zero dependencies, backed by SQLite) that
exposes **generic REST endpoints**. You — the coding agent — define and freely
reshape the data schema through one set of endpoints, while the frontend you
build reads and writes through another fixed set. The frontend never changes
the *endpoints* it calls, even as you iterate the schema dozens of times.

The whole point: **schema changes are O(1) and instant** (lazy invalidation —
no migrations, no rewriting rows), so you can iterate the data model as fast as
you iterate the UI.

## When to use it

Use MorphDB when building a small data-backed web app and you want persistence
without committing to a rigid backend early. Todo apps, trackers, CRMs,
dashboards, inventory tools, note apps — anything that is "CRUD + relationships".

Do **not** reach for it when the user already has a real backend/database, or
needs multi-tenant auth, horizontal scale, or strong durability guarantees.
It is a localhost-scale dev tool.

## Start the server

```bash
# from the morphdb repo (no install needed — pure stdlib):
python3 -m morphdb --port 8787 --db ./app.sqlite3
# or, if installed:  morphdb --port 8787 --db ./app.sqlite3
```

It serves on `http://127.0.0.1:8787`. Data persists in the SQLite file. CORS is
wide open, so a frontend served from any origin (a `file://` page, a Vite dev
server, etc.) can call it directly.

`curl http://127.0.0.1:8787/help` prints the full live endpoint reference.

## The two-layer workflow

### 1. Define the schema (you, the agent)

Object types have typed fields. Field types: `string`, `number`, `boolean`,
`json`, `datetime`.

```bash
curl -X POST http://127.0.0.1:8787/schemas/objects -d '{
  "name": "task",
  "fields": { "title": "string", "done": "boolean", "priority": "number" }
}'
```

Change your mind later? Just morph it — existing data is untouched and reread
through the new schema:

```bash
# add a field (merge keeps the rest)
curl -X PUT http://127.0.0.1:8787/schemas/objects/task \
  -d '{ "merge": true, "fields": { "due": "datetime", "tags": "json" } }'

# drop a field (its data is hidden, not destroyed — re-add it to recover values)
curl -X POST http://127.0.0.1:8787/schemas/objects/task/delete-fields \
  -d '{ "fields": ["priority"] }'
```

Relationships are "association types" with a cardinality
(`one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`) and a human label
for each direction:

```bash
curl -X POST http://127.0.0.1:8787/schemas/associations -d '{
  "name": "assignment", "from_type": "user", "to_type": "task",
  "forward_name": "tasks", "inverse_name": "assignee",
  "cardinality": "one_to_many"
}'
```

### 2. Read & write data (the frontend you build)

Every object gets a globally unique `_guid`. System fields are `_guid`, `_type`,
`_created_at`, `_updated_at`; everything else is your fields, flat.

```bash
# create  -> returns the object incl. _guid
curl -X POST http://127.0.0.1:8787/objects/task -d '{"title":"buy milk","done":false}'

# read one
curl http://127.0.0.1:8787/objects/task/<guid>

# list + query (operators: __gt __gte __lt __lte __ne __contains __in __exists)
curl "http://127.0.0.1:8787/objects/task?done=false&sort=priority&order=desc&limit=20"

# patch (merge) / put (replace) / delete
curl -X PATCH  http://127.0.0.1:8787/objects/task/<guid> -d '{"done":true}'
curl -X DELETE http://127.0.0.1:8787/objects/task/<guid>

# link two objects, then traverse
curl -X POST http://127.0.0.1:8787/associations \
  -d '{"assoc_name":"assignment","from_guid":"<user>","to_guid":"<task>"}'
curl "http://127.0.0.1:8787/object/<user>/associations?relation=tasks&expand=true"
```

## Recipe for building an app

1. Start MorphDB pointed at a project-local `.sqlite3` file.
2. Sketch the object types and define them with `POST /schemas/objects`.
3. Build the frontend (plain `fetch`) against the generic data endpoints.
   Point all calls at `http://127.0.0.1:8787`.
4. When the UI needs a new field or relationship, morph the schema with **one**
   call — do not rewrite stored data, do not touch the frontend's endpoint URLs.
5. Have the frontend ensure its own schema on load (idempotent `PUT
   /schemas/objects/<type>`), so the app is self-bootstrapping.

## Gotchas

- Writing a field not in the schema is rejected (400) — add it to the schema
  first. This catches typos early.
- Values are coerced to the declared type; `"yes"`/`1` → boolean `true`, numeric
  strings → numbers. A boolean for a `number` field is rejected.
- Associations enforce cardinality and return 409 on conflict; pass
  `?replace=true` to steal an existing exclusive edge.
- Self edges (`from_guid == to_guid`) are rejected.
- Deleting an object type cascades to its objects and their edges by default.

See the repo `README.md` for the complete reference.
