---
name: morphdb
description: A coding-agent-friendly, multi-tenant backend for vibe-coded websites. Activate whenever you are building or iterating on a website or web app that needs a backend — anything that must store, save, persist, or query data, wants a database or REST API, or needs to remember state across reloads (todo apps, CRMs, dashboards, trackers, notes, booking, inventory, any CRUD or data-driven site) — instead of hand-writing and re-migrating a database as the design churns. You reshape each app's data model by running the MorphDB CLI (`morphdb schema add-field`, `morphdb schema add-relation`, …); the frontend you build calls the same generic, stable HTTP object endpoints no matter how the schema changes. One MorphDB process backs many isolated apps (one per site). Also use it to start/stop/debug a site's MorphDB backend. Trigger on "build a backend for this", "make it save data", "add a database", "persist this", a site that loses data on refresh, or seeing a MorphDB marker comment in a project.
---

# MorphDB — instant morphable backend for AI-built apps

MorphDB is a single Python process. By default it is zero-dependency and backed
by SQLite; it can also run against PostgreSQL or DynamoDB when configured. One
process hosts **many apps** — one per website you build — fully isolated from
each other. Three surfaces:

- **App** — the tenant. Register one with a key you choose; every later call
  carries that key. You only ever touch the app whose key you hold — there is no
  "list apps".
- **Schema** — the data model *within your app*. **You, the agent, reshape it by
  running the `morphdb` CLI** (`morphdb schema add-field`, `morphdb schema
  add-relation`, `morphdb schema show`, …).
- **Objects** — the data *within your app*. **The frontend you build** reads and
  writes it over plain HTTP (`fetch`), sending the same app key as a header.

So: **schema = your `morphdb` commands; data = the frontend's HTTP calls.** Schema
changes are **O(1) and instant** (lazy invalidation — no migrations, no rewriting
rows), so you iterate the data model as fast as you iterate the UI, and the
frontend never changes which endpoints it calls.

## When to use it

Small data-backed web app that wants persistence without a rigid backend: todos,
trackers, CRMs, dashboards, inventory, notes — "CRUD + relationships".

Do **not** use it when the user already has a real backend/database, or needs
production-grade auth, permissions, high-throughput horizontal scale, or strict
durability/SLA guarantees without deliberately designing the deployment around
those needs. Local SQLite is the default prototype path; PostgreSQL and DynamoDB
targets are available when the user wants a shared or hosted MorphDB server.

## The morphdb CLI (how you reshape the data model)

You reshape the schema by running the **`morphdb`** command (`pip install
morphdb`). Two things to do once per session:

1. **Start the backend:** `morphdb start` (background; default `127.0.0.1:8787`).
   The schema commands and your frontend both talk to it over HTTP. If a command
   says it can't reach the backend, run `morphdb start`.
2. **Set the app key:** `export MORPHDB_APP=<your-key>` — then every command below
   uses it automatically and you can omit `--app`. (Or pass `--app <key>` per
   command, after the subcommand.)

| Command | What it does |
| --- | --- |
| `morphdb app register <key>` | Create an app (your unique key). 409 if taken. **Remember the key.** |
| `morphdb app delete <key>` | Delete an app and cascade-delete everything under it. |
| `morphdb schema list` | List every type with its fields + relations + inverse relations. |
| `morphdb schema show <type>` | Show one type's full schema. |
| `morphdb schema add-field <type> <name> <field_type> [--default V] [--required] [--index]` | Add/update a field. Idempotent; creates the type if new. `--index` makes it filterable/sortable. |
| `morphdb schema drop-field <type> <name>` | Remove a field (values hidden, not destroyed). |
| `morphdb schema add-relation <type> <name> --to T --cardinality C [--inverse I] [--symmetric] [--description D] [--inverse-description ID]` | Declare a relation (inverse appears automatically on the other type). |
| `morphdb schema drop-relation <type> <name>` | Remove a relation + its edges (drop from the authoring side). |
| `morphdb schema delete-type <type>` | Delete a type, its objects, and their edges. |
| `morphdb schema set <type> --json '{…}'` | Escape hatch: apply a raw `{fields?, relations?, merge?}` doc. |
| `morphdb query <type> ['<querystring>']` | Read-only peek at objects, for **debugging** (the frontend does normal reads itself). |

`field_type` ∈ `string`, `number`, `boolean`, `json`, `datetime`.
`cardinality` ∈ `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many`
(`X_to_Y` → the *from* side sees `Y`, the *to* side sees `X`).

> **Modeling rule — links are relations, not id fields. Think ORM, not raw SQL.**
> When one object points at another — a task's assignee, a stage's business, a
> review's book — declare a **relation** (`morphdb schema add-relation`), never a
> `string`/`json` field holding the other object's guid. A relation *is* MorphDB's
> foreign key: it is **filterable** like one — `GET /objects/stage?business=<guid>`,
> index-backed via the edge table, composable with field filters, `sort`, and
> pagination — and it also gives you the **inverse for free** (one read traverses
> both ways: `business.stages` and `stage.business`) and keeps edges
> cascade-clean. A raw id-field has none of that — no inverse, no cascade, and it
> is not filterable at all unless you index it. Use a field only for a genuine
> scalar you store *on* the object (title, price, status, a timestamp); anything
> that references another object is a relation. (Filter a relation with
> `?rel=<guid>`, `__in`, `__ne`, `__exists`.)

> **Indexing rule — pass `--index` to filter or sort on a field.** Fields are
> storage-only by default. A field filter (`?status=…`, `__gt`/`__lt`/`__in`/
> `__contains`/`__exists`) or `sort=field` on an **un-indexed** field is a hard
> error telling you to index it. Index the few fields you actually query (a status,
> a priority, a timestamp); leave the rest (long text, json blobs) un-indexed so
> writes stay cheap. Turning the flag on backfills existing objects automatically;
> turning it off is instant. `json` can't be indexed; relations are always
> filterable and need no flag.

## 0. Register your app first (once)

Everything is scoped to an app. Pick a unique key, register it, and **remember
it** — there is no way to read it back.

1. `morphdb app register my-cool-site` (409 means the key is already taken — pick
   another).
2. `export MORPHDB_APP=my-cool-site` so every later command uses it, and **bake the
   same key into the frontend** as `window.MORPHDB_APP` (it rides on the
   `X-App-Key` header).
3. Leave a marker comment in the site (see below) so a later session recovers the
   key and re-activates this skill.

Deleting an app cascades: `morphdb app delete my-cool-site` wipes its schema,
objects, and relations in one shot (other apps are untouched).

## 1. Reshape the schema by running the CLI (you, the agent)

A worked example — a todo app with users (with `MORPHDB_APP` exported):

- `morphdb schema add-field task title string`
- `morphdb schema add-field task done boolean --default false --index` — filtered, so indexed
- `morphdb schema add-field task priority number --index` — sorted, so indexed
- A relation, declared once on `task`; `user.tasks` then appears automatically.
  Many tasks → one user, so `task.assignee` is one guid and `user.tasks` is a
  list: `morphdb schema add-relation task assignee --to user --cardinality many_to_one --inverse tasks`
- A mutual relation within one type (friends): one shared label, pass `--symmetric`:
  `morphdb schema add-relation user friends --to user --cardinality many_to_many --symmetric`
- Inspect / change: `morphdb schema show task`, `morphdb schema list`,
  `morphdb schema drop-field task priority`, `morphdb schema drop-relation task assignee`,
  `morphdb schema delete-type task`.

`add-field` is an idempotent merge, so re-running it is safe. For anything the
named commands don't cover, use `morphdb schema set` with a raw document, e.g.
`morphdb schema set task --json '{"merge": true, "fields": {"due": "datetime"},
"relations": {"tags": {"to": "tag", "cardinality": "many_to_many", "inverse":
"tasks"}}}'`.

## 2. Read & write data through the object endpoints (the frontend you build)

The **object endpoints** are the stable, generic HTTP surface the frontend calls
at runtime — they never change as you morph the schema. The *frontend* calls
them (write `fetch`), not you; relations are just fields on the object body (a
guid for to-one, a list of guids for to-many). Use `morphdb query` only when
*you* need to inspect data while debugging.

### The object endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /objects/{type}` | Create. Body = field + relation values. Returns the object with `_guid`. |
| `GET /objects/{type}` | List/query. Field filters `?field=…` (`__gt/gte/lt/lte/ne/contains/in/exists`, **indexed fields only**); **relation filters** `?rel=<guid>` (`rel__in/__ne/__exists`); **`include`** to nest relations; `sort`, `order`, `limit`, `offset`. |
| `GET /objects/{type}/{guid}` | Read one (type-checked). `?include=…` nests relations. |
| `GET /object/{guid}` | Read one by guid alone. Supports `?include=…`. |
| `PATCH /objects/{type}/{guid}` | Merge fields; set any relations present (create if absent). |
| `PUT /objects/{type}/{guid}` | Replace fields; set any relations present (create if absent). |
| `DELETE /objects/{type}/{guid}` | Delete object + its edges (neighbors survive). |

List returns `{objects, total, limit, offset}` (`total` = full filtered count).
On DynamoDB, MorphDB keeps this exact behavior, but large offsets, broad exact
totals, `__contains`, negative relation filters, and broad sorts may read more
items internally. Prefer selective indexed filters and small offsets when you
know the backend is DynamoDB.

**Nested reads — `include`.** By default a relation comes back as a guid (to-one)
or list of guids (to-many). Add `?include=<rel>,<rel>.<subrel>` to replace those
with the full neighbor object(s), nested Prisma-style — `?include=author,comments.author`
(comma-separated paths, dots go deeper). Read-only, depth ≤ 4, batched (no N+1).
Writes stay flat: create/update with guids, never nested objects.

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

// …or hydrate relations into nested objects with include (Prisma-style)
await db("GET", `/objects/task/${task._guid}?include=assignee`); // assignee = {…full user}
await db("GET", `/objects/user/${ann._guid}?include=tasks`);     // tasks = [{…full task}]

// list + query (field filters)
const { objects, total } = await db("GET",
  "/objects/task?done=false&sort=priority&order=desc&limit=20");

// filter by a relation — like a foreign key. Tasks assigned to one user, AND
// not done. Index-backed (resolved via the edge table), composable with fields.
const mine = await db("GET",
  `/objects/task?assignee=${ann._guid}&done=false`);
// also: ?assignee__in=g1,g2 · ?assignee__exists=false (unassigned) · __ne

// update — relations are set-as-field (the value becomes the whole set)
await db("PATCH", `/objects/task/${task._guid}`, { done: true });
await db("PATCH", `/objects/task/${task._guid}`, { assignee: otherUserGuid }); // re-link (last write wins)
await db("PATCH", `/objects/user/${ann._guid}`,  { tasks: [t1, t2] });          // set the whole set
await db("PATCH", `/objects/task/${task._guid}`, { assignee: null });           // clear
await db("DELETE", `/objects/task/${task._guid}`);
```

## Mental model

A **type** is one schema document: `fields` (raw values) + `relations` (links to
other types). An **object** is an instance with a `_guid`. A **relation** is
declared once on one type and is read/written **like a field** on the object —
a neighbor guid (to-one) or a list of guids (to-many). It shows up on both types
automatically (the inverse).

System fields on every object: `_guid`, `_type`, `_created_at`, `_updated_at`.

## Recipe for building an app

1. **Start the backend** (`morphdb start`) and **register an app**
   (`morphdb app register my-cool-site`); `export MORPHDB_APP=my-cool-site` and bake
   the key into the frontend (`window.MORPHDB_APP`).
2. Define the object types and relations with the schema commands
   (`morphdb schema add-field`, `morphdb schema add-relation`, …).
3. Build the frontend (plain `fetch`, the `db()` client above) against
   `/objects/...` — relations are fields, so the UI just reads/writes guids.
4. When the UI needs a new field or relation, run **one** command — no data
   rewrite, no change to the frontend's endpoint URLs.
5. Leave a MorphDB marker comment in the site (below) so future iterations
   re-activate this skill and remember the app key.

## Managing / debugging the backend

Start the backend once (`morphdb start`); the schema commands and your frontend
both talk to it. Useful commands:

```bash
morphdb start      # start the background server (default 127.0.0.1:8787)
morphdb status     # running? where? how many apps?
morphdb stop       # stop it
morphdb logs -f    # follow the server log
morphdb dashboard  # read-only web view of every app + SQL/logical tables
```

Database target examples:

```bash
morphdb start --db ./app.sqlite3
morphdb start --db postgresql://user:pass@host:5432/db
morphdb start --db 'dynamodb://morphdb-prod?region=us-west-2'
```

For DynamoDB install `morphdb[dynamodb]`; credentials come from the normal AWS
IAM/boto3 chain, not from a database password in the URL. Use
`?create_table=true` only for local/prototype table creation. Use
`endpoint_url=http://localhost:8000` only for DynamoDB Local/LocalStack; omit
`endpoint_url` when targeting real AWS DynamoDB.

**Debug tip:** if the frontend can't reach the backend (connection refused, a
`fetch` throws) and you're running locally, the server is probably down — run
`morphdb status`, then `morphdb start`, and check `morphdb logs`. A `morphdb query
<type>` command is the quickest way to confirm what data actually got written —
e.g. `morphdb query task 'done=false&sort=priority&limit=20'`.

**Using a hosted MorphDB instead of localhost.** If the `MORPHDB_HOST` env var is
set, it is the URL of a MorphDB server hosted elsewhere; the `morphdb` commands
target it (no local server needed). Bake the same URL into the frontend as
`window.MORPHDB_HOST` so its `fetch` calls hit the hosted backend. It accepts a
full URL (`https://db.example.com`) or a bare `host:port`, and always points at a
*backend* — never a database directly (a browser can't reach a database, only an
API).

## Shipping a schema with the repo (rare — only when asked)

**Skip this unless the user explicitly asks to export, snapshot, share, or stand
up a schema** — e.g. "export the schema so others can run this repo", or setting
up a cloned MorphDB-backed project on a fresh instance. Almost every session
ignores it: the schema already lives in the running backend, so don't reach for
these proactively.

A schema otherwise lives only inside the one MorphDB that holds it. The portable
form is `morphdb.schema.json` committed at the website's repo root — self-contained
JSON (the app key, every type, its fields and relations). Two commands move it:

- **Export** — snapshot a running app's data model to that file:
  `morphdb export-schema <app> > morphdb.schema.json`
- **Init** on any instance — stand the app up from the file (the app key comes
  from inside it). Idempotent; the default file is `./morphdb.schema.json`:
  `morphdb init`
  - App missing → created and the schema applied.
  - App exists → the schema is **merged in additively; existing data is kept**. A
    name clash never deletes — re-running just converges the schema.
  - `morphdb init --reset` → the only destructive path: delete the app and rebuild
    it clean (prompts when interactive). Only the schema travels — object data is
    not moved.

## Leave a breadcrumb in the site

So this skill re-activates whenever you (or a later session) iterate on the app,
drop an explicit note that the site is MorphDB-backed — a comment near the top of
the main HTML / entry file (and/or a line in the project README):

```html
<!-- Backend: MorphDB · app key "my-cool-site".
     Schema edits: the `morphdb schema` CLI (add-field, add-relation, …).
     Server: `morphdb start` / `morphdb status` / `morphdb logs`.
     This project uses the `morphdb` agent skill. -->
```

It reminds the agent which app key to use and signals that the `morphdb` skill
applies here — cheap context that keeps later edits consistent.

## Gotchas

- Every object request from the frontend needs the `X-App-Key` header (the `db()`
  client above sends it). Missing → 400; unknown key → 404. Register the app
  first. Type names are unique only *within* an app — reuse across apps is fine.
- Writing a field/relation not in the schema is rejected (400) — declare it first
  with a `morphdb schema` command. Catches typos early.
- Values are coerced to the declared type; `"yes"`/`1` → boolean `true`, numeric
  strings → numbers. A boolean for a `number` field is rejected.
- A relation may not share a name with a field on the same type. A non-symmetric
  self-relation needs distinct forward/inverse names (or pass `--symmetric`).
- The **inverse** of a relation shows up on the *other* type, not in the
  `add-relation` output — `morphdb schema show` the target type to see it (declare
  `order.customer`, and `customer.orders` appears on `customer`). Inverse names only
  need to be unique on the type they land on, so two relations can reuse an inverse
  label when they point at **different** target types (`review.book` and
  `review.author` can both use inverse `reviews`).
- Setting a relation is set-as-field (the value becomes its full set). For a
  single-valued slot already taken, **last write wins** (the old link is moved).
  `null`/`[]` clears.
- Deleting an object removes only its edges; neighbor objects survive. Deleting a
  *type* removes its own objects + their edges, but not neighbor objects.
- After a field **retype**, an old-typed value reads as unset (default/null) until
  rewritten.
- Relations **are** filterable on the list endpoint, like an ORM foreign key:
  `?rel=<guid>` plus `__in`/`__ne`/`__exists`, index-backed via the edge table and
  composable with field filters/sort/pagination. Only scalar comparisons
  (`gt`/`lt`/`contains`) are field-only. So model a link as a relation, not a
  guid-bearing string field.
- DynamoDB uses the same MorphDB API as SQLite/Postgres, not a narrower
  DynamoDB-specific API. It is correct for exact totals and offset pagination,
  but some broad query shapes cost more. Shape generated apps toward selective
  indexed filters, relation equality filters, and "load more" pagination when
  possible.

See the repo `README.md` for the complete reference.
