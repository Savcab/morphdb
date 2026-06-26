# MorphDB examples

Each folder is a **complete, single-file web app** (`index.html`, no build step, no
dependencies) backed by MorphDB — and each ships the **`morphdb.schema.json`** that
defines its data model. The schema file carries the app key, so anyone can stand the
backend up on their own MorphDB with one command and run the frontend unchanged.

That's the whole point of MorphDB: **one generic backend, many apps.** Every app
below talks to the *same* fixed `/objects/...` REST endpoints — only the schema
differs. A LinkedIn-style network, a Notion-style doc tree, and a Figma-style canvas
are the same backend with different `morphdb.schema.json` files.

## The gallery

| Folder | App | Models a… | MorphDB features it shows off | Types |
| --- | --- | --- | --- | --- |
| [`todo`](todo/) | **Todo** | a to-do list | the minimum: one type, self-bootstrap | `todo` |
| [`linkedin`](linkedin/) | **Linkup** | LinkedIn-style network | **symmetric many-to-many** connections, `include=author` hydration, indexed filter/sort | `person`, `post`, `job` |
| [`notion`](notion/) | **Noted** | Notion-style nested docs | **self-referential hierarchy** (`page.parent → page`), ordered children, `include=` | `page`, `block` |
| [`figma`](figma/) | **Shaper** | Figma-style design canvas | **high-frequency spatial `PATCH`** (drag = persist x/y/z), relation filter | `doc`, `shape` |
| [`asana`](asana/) | **Boardly** | Asana-style project board | **multiple relations per type**, group-by-relation, `include=assignee` | `user`, `project`, `section`, `task` |
| [`linear`](linear/) | **Stride** | Linear-style issue tracker | **indexed enum status** driving a kanban, priority sort, `include=assignee` | `user`, `team`, `issue`, `comment` |

> These are original, educational clones — "*X*-style" examples with their own names
> and marks, not affiliated with or endorsed by those products.

## Run one

Start MorphDB, then either let the page stand its own backend up, or do it explicitly
with the schema file.

**Easiest — just serve the folder.** The page registers its app and applies its own
`morphdb.schema.json` on first load (the in-browser equivalent of `morphdb init`),
then seeds sample data so it's never blank:

```bash
morphdb start                          # the generic backend, on 127.0.0.1:8787
cd examples/linear
python3 -m http.server 8000            # serve over http so the page can read its schema file
# open http://localhost:8000
```

**Or stand the backend up from the schema file first** — this is the portable path,
the thing you'd run on a fresh clone or a teammate's MorphDB:

```bash
morphdb start
morphdb init examples/linear/morphdb.schema.json   # creates the app + applies the schema (idempotent)
cd examples/linear && python3 -m http.server 8000  # then open http://localhost:8000
```

`morphdb init` is safe to re-run: it **merges** into an existing app and never deletes
data — only `morphdb init --reset` rebuilds clean. Point any of these at a hosted
MorphDB instead of localhost by exporting `MORPHDB_HOST=https://your-host` before
`morphdb init` (the page targets `:8787` on whatever host serves it).

## Build your own

Copy any folder as a starting point. Reshape its `morphdb.schema.json` (add a field,
add a relation) however your UI wants — the frontend keeps calling the same generic
endpoints. When you're happy, `morphdb export-schema <app> > morphdb.schema.json`
snapshots the live model back into the file so it ships with your repo.
