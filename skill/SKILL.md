---
name: morphdb
description: A coding-agent-friendly, multi-tenant backend for vibe-coded websites. Use when building an HTML/CSS/JS frontend that needs to persist and query data (todos, CRM, dashboard, tracker, any CRUD app) but you do not want to hand-write and re-migrate a database schema as the design churns. One MorphDB process hosts many isolated apps (one per site); you reshape each app's schema with one command and the frontend keeps calling the same generic, stable REST endpoints. Trigger when the user asks for a "backend for this", "make it save data", "add a database", or "persist this" for a small/local web app.
---

# MorphDB — instant morphable backend for AI-built apps

MorphDB is a single Python process (zero dependencies, backed by SQLite). One
process hosts **many apps** — one per website you build — fully isolated from
each other. Three surfaces:

- **App** — the tenant. Register one with a key you choose; every schema/object
  call then carries that key in the `X-App-Key` header. You only ever touch the
  app whose key you hold — there is no "list apps".
- **Schema** — the data model *within your app*. *You*, the agent, reshape it
  with the `morphdb_schema` CLI (don't hand-write curl for schema edits).
- **Objects** — the data *within your app*. The *frontend you build* reads/writes
  it over plain HTTP (`fetch`/curl), sending the same `X-App-Key`.

Schema changes are **O(1) and instant** (lazy invalidation — no migrations, no
rewriting rows), so you iterate the data model as fast as you iterate the UI,
and the frontend never changes which endpoints it calls.

## When to use it

Small data-backed web app that wants persistence without a rigid backend: todos,
trackers, CRMs, dashboards, inventory, notes — "CRUD + relationships".

Do **not** use it when the user already has a real backend/database, or needs
multi-tenant auth, horizontal scale, or strong durability. It is a
localhost-scale dev tool.

## Start the server

```bash
# from the morphdb repo (no install needed — pure stdlib):
python3 -m morphdb --port 8787 --db ./app.sqlite3
```

Serves on `http://127.0.0.1:8787`. Data persists in the SQLite file. CORS is
wide open, so a frontend on any origin can call it. `curl .../help` prints the
live endpoint reference.

## Mental model

A **type** is one schema document: `fields` (raw values) + `relations` (links to
other types). An **object** is an instance with a `_guid`. A **relation** is
declared once on one type and is read/written **like a field** on the object —
a neighbor guid (to-one) or a list of guids (to-many). It shows up on both
types automatically (the inverse).

System fields on every object: `_guid`, `_type`, `_created_at`, `_updated_at`.
Field types: `string`, `number`, `boolean`, `json`, `datetime`.

## 0. Register your app first (once)

Everything is scoped to an app. Pick a unique key, register it, and **remember
it** — there is no endpoint to read it back.

```bash
S="python3 skill/scripts/morphdb_schema.py"   # adjust path to the skill dir
$S register-app my-cool-site                  # 409 if the key is already taken
export MORPHDB_APP=my-cool-site               # the CLI sends this as X-App-Key
```

Persist the key where the project will find it again (the `MORPHDB_APP` env var
for the CLI; a `window.MORPHDB_APP` constant in the frontend). Deleting an app
cascades — `$S delete-app my-cool-site` wipes its schema, objects, and relations
in one shot (other apps are untouched).

## 1. Reshape the schema with the CLI (you, the agent)

Use `skill/scripts/morphdb_schema.py`. It talks to MorphDB at
`http://127.0.0.1:8787` by default; override with the `MORPHDB_HOST` env var (a
full URL, or a bare `host:port`) or the `--url` flag. Every command runs against
the app in `$MORPHDB_APP` (or `--app KEY`). Don't curl the schema endpoints by
hand.

```bash
S="python3 skill/scripts/morphdb_schema.py"      # adjust path to the skill dir

# create / extend a type — add-field is idempotent (merge), so safe to re-run
$S add-field task title  string
$S add-field task done   boolean --default false
$S add-field task priority number

# a relation: declared once on `task`; `user.tasks` appears automatically.
# many tasks → one user, so task.assignee is one guid and user.tasks is a list.
$S add-relation task assignee --to user --cardinality many_to_one --inverse tasks

# a mutual relation within one type (friends): symmetric, one shared label
$S add-relation user friends --to user --cardinality many_to_many --symmetric

# inspect / drop
$S show task
$S list
$S drop-field   task priority      # data hidden, not destroyed; re-add to recover
$S drop-relation task assignee     # also removes its edges
$S delete-type  task               # type + its objects + their edges (neighbors survive)
```

For anything the subcommands don't cover, send a raw schema document:

```bash
$S set task --json '{"merge":true,"fields":{"due":"datetime"},
  "relations":{"tags":{"to":"tag","cardinality":"many_to_many","inverse":"tasks"}}}'
```

Cardinalities: `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`
(`X_to_Y` → the *from* side sees `Y`, the *to* side sees `X`).

## 2. Read & write data through the object endpoints (the frontend you build)

The **object endpoints** are the stable, generic surface the frontend calls at
runtime — they never change as you morph the schema. The frontend is what calls
them, so write `fetch` against them (don't route FE data access through the
schema CLI; that's for you, the agent, editing the model).

Relations are just fields on the object body: a guid for to-one, a list of guids
for to-many.

### The object endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /objects/{type}` | Create. Body = field + relation values. Returns the object with `_guid`. |
| `GET /objects/{type}` | List/query. `?field=…`, `field__gt/gte/lt/lte/ne/contains/in/exists`, `sort`, `order`, `limit`, `offset`. |
| `GET /objects/{type}/{guid}` | Read one (type-checked). |
| `GET /object/{guid}` | Read one by guid alone. |
| `PATCH /objects/{type}/{guid}` | Merge fields; set any relations present (create if absent). |
| `PUT /objects/{type}/{guid}` | Replace fields; set any relations present (create if absent). |
| `DELETE /objects/{type}/{guid}` | Delete object + its edges (neighbors survive). |

List returns `{objects, total, limit, offset}` (`total` = full filtered count).

### A drop-in FE client

```js
// MorphDB object-endpoint client — paste into the frontend you build.
// Defaults to localhost:8787. Set window.MORPHDB_HOST (full URL or host:port)
// and window.MORPHDB_APP (your app key) in a <script> before this runs to
// override. Every request sends the X-App-Key header.
const MORPHDB_HOST = (typeof window !== "undefined" && window.MORPHDB_HOST) || "127.0.0.1:8787";
const MORPHDB_APP  = (typeof window !== "undefined" && window.MORPHDB_APP)  || "my-cool-site"; // the key you registered
const BASE = MORPHDB_HOST.includes("://") ? MORPHDB_HOST : "http://" + MORPHDB_HOST;

async function db(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", "X-App-Key": MORPHDB_APP },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = res.status === 204 ? null : await res.json();
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
  return data;
}

// create — set relations inline, just like fields
const ann  = await db("POST", "/objects/user", { name: "Ann" });
const task = await db("POST", "/objects/task",
  { title: "buy milk", done: false, assignee: ann._guid });

// read — relations come back as guids (task.assignee; and on the user: tasks)
await db("GET", `/objects/task/${task._guid}`);   // { …, assignee: "user_…" }
await db("GET", `/objects/user/${ann._guid}`);     // { …, tasks: ["task_…"] }

// list + query
const { objects, total } = await db("GET",
  "/objects/task?done=false&sort=priority&order=desc&limit=20");

// update — relations are set-as-field (the value becomes the whole set)
await db("PATCH", `/objects/task/${task._guid}`, { done: true });
await db("PATCH", `/objects/task/${task._guid}`, { assignee: otherUserGuid }); // re-link (last write wins)
await db("PATCH", `/objects/user/${ann._guid}`,  { tasks: [t1, t2] });          // set the whole set
await db("PATCH", `/objects/task/${task._guid}`, { assignee: null });           // clear
await db("DELETE", `/objects/task/${task._guid}`);
```

Quick manual poke from the shell (same endpoints):

```bash
B=http://127.0.0.1:8787 ; H="X-App-Key: my-cool-site"
curl -X POST $B/objects/task -H "$H" -d '{"title":"buy milk","done":false}'
curl -H "$H" "$B/objects/task?done=false&sort=priority&order=desc&limit=20"
```

## Recipe for building an app

1. Start MorphDB pointed at a project-local `.sqlite3` file.
2. **Register an app** (`$S register-app <key>`), set `MORPHDB_APP`, and bake the
   same key into the frontend (`window.MORPHDB_APP`) — it rides on `X-App-Key`.
3. Define the object types and relations with the `morphdb_schema` CLI.
4. Build the frontend (plain `fetch`) against `/objects/...` — relations are
   fields, so the UI just reads/writes guids.
5. When the UI needs a new field or relation, run **one** CLI command — no data
   rewrite, no change to the frontend's endpoint URLs.

## Gotchas

- Every schema/object call needs the `X-App-Key` header (the CLI and the FE
  client above send it for you). Missing → 400; unknown key → 404. Register the
  app first. Type names are unique only *within* an app — reuse across apps is fine.
- Writing a field/relation not in the schema is rejected (400) — declare it
  first. Catches typos early.
- Values are coerced to the declared type; `"yes"`/`1` → boolean `true`, numeric
  strings → numbers. A boolean for a `number` field is rejected.
- A relation may not share a name with a field on the same type. A non-symmetric
  self-relation needs distinct forward/inverse names (or use `--symmetric`).
- Setting a relation is set-as-field (the value becomes its full set). For a
  single-valued slot already taken, **last write wins** (the old link is moved).
  `null`/`[]` clears.
- Deleting an object removes only its edges; neighbor objects survive. Deleting
  a *type* removes its own objects + their edges, but not neighbor objects.
- After a field **retype**, an old-typed value reads as unset (default/null)
  until rewritten. Filtering is on fields, not relations.

See the repo `README.md` for the complete reference.
