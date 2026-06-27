# MorphDB limitations found while building the example apps

An append-only log of **real limitations of the MorphDB backend** surfaced by
building/auditing the example clones (LinkedIn, Notion, Figma, Linear).
These are things the apps couldn't do cleanly because the *generic backend*
lacks a primitive — not frontend bugs (those get fixed in the apps). Each entry:
what we hit, where it bit, and a possible backend direction.

> Status: living document. New findings are appended as the audit loop runs.

---

<!-- APPEND NEW FINDINGS BELOW THIS LINE -->

## 1. No value-level validation (only field *type* is enforced)
*Surfaced by: Figma (fill, w/h).*
The store validates a field's **type** (it rejects `w:"abc"` with `400 Field w expects a number`) and rejects unknown fields, but it cannot constrain a value: no format (hex color), no numeric range/min, no string enum/length, no "allowed values" for a free-form `kind` field. So `fill:"banana"` and `w:-150` are accepted and persisted, collapsing a shape to 0px. **All semantic validation must be done client-side.** A backend that accepted optional per-field constraints (regex/min/max/enum) would catch these centrally.

## 2. No atomic add/remove on a to-many relation (set-add / array-append)
*Surfaced by: LinkedIn (connections), Notion/Figma (any list membership).*
A to-many relation is written by sending its **whole** new array. To add one connection you read the full `connections` list and PATCH it back — a read-modify-write that **loses updates** when two edits overlap (connect to two people in quick succession → one is dropped, server-confirmed). A `{add:[guid], remove:[guid]}` delta op on a relation would make membership edits atomic and concurrency-safe.

## 3. No atomic increment / counter primitive
*Surfaced by: LinkedIn (post likes).*
A counter like `likes` is a scalar the client reads, `+1`s, and writes back. Concurrent likes race (last-write-wins), and there's no server-side `increment` op. A `PATCH {likes: {$inc: 1}}`-style primitive would fix it.

## 4. No atomic reorder / multi-object transaction
*Surfaced by: Figma (z-order), Notion (block order), Linear/Asana (ordered lists).*
Ordering is emulated with an integer `order`/`z` field that the client computes (`max(z)+1`) and PATCHes one object at a time. There is no atomic "move item to position N" and no way to renumber a whole list in one transaction, so concurrent reorders can collide and a full renumber is N separate writes (non-atomic).

## 5. No realtime / subscriptions (no push, observe, websocket, or SSE)
*Surfaced by: all (multi-tab), Figma header reads "synced" but isn't live.*
The client is request/response `fetch` only. Two tabs or two users never see each other's edits without a manual reload. The generic backend offers no change-feed/observe primitive, so a collaborative "live" experience (the whole point of Figma/Notion) can't be built on it.

## 6. No full-text / substring search
*Surfaced by: Figma (search layers), would hit LinkedIn/Notion search too.*
List filtering is **exact-equality** on indexed fields (plus `contains` only where explicitly supported per field). There's no general full-text or fuzzy search across an app's objects, so a global search box (a core feature of every one of these products) isn't expressible against the backend.

## 7. No server-side aggregation / group-by / count-by
*Surfaced by: Figma ("Layers · N"), Linear (per-status counts), LinkedIn (counts).*
List responses expose a flat `total` for the filter, but no grouped counts (e.g. shapes-per-kind, issues-per-status, connections-count without fetching them all). Every tally is computed client-side after fetching the objects, which doesn't scale.

## 8. App keys are namespaces, not end-user identity/auth
*Surfaced by: LinkedIn (idempotent per-user likes, "your" posts).*
`X-App-Key` isolates apps but is not authentication and carries no per-user identity. There's no notion of "the current user," so per-user semantics (one like per user, ownership of a post, permissions) must be faked client-side (here: localStorage), which is trivially bypassed. Real multi-user apps need an identity/auth primitive.

## 9. No client-supplied object IDs, and no soft-delete + restore
*Surfaced by: Figma (undo of a delete).*
The server assigns every `_guid` on create; a client can't create an object with a known id, and there's no soft-delete/restore. So "undo a delete" can't bring the *same* object back — re-creating yields a **new** `_guid`, and any references to the old guid must be remapped client-side (the Figma undo stack does exactly this across its history snapshots + selection). A client-supplied id (idempotent create) or a soft-delete/restore (trash + undelete) primitive would make undo/redo and offline replay correct instead of approximate.

## 10. No cascade delete / bulk-delete-by-filter
*Surfaced by: Notion (delete a page + its subtree).*
Deleting a type's objects is one-at-a-time. Deleting a Notion page must walk the subtree client-side and issue a `DELETE` per descendant page **and** per block (the app does exactly this). It's non-atomic — a mid-way failure orphans blocks/pages — and O(N) round-trips. (App-level delete *does* cascade its own schema/objects, but there's no object-level `ON DELETE CASCADE` for a relation, and no "delete all objects matching this filter" endpoint.)

## 11. No uniqueness constraint and no sequence / auto-increment
*Surfaced by: Linear (duplicate `ENG-11` minted).*
There's no unique index and no atomic sequence allocator. An issue tracker needs unique, monotonic identifiers (ENG-1, ENG-2, …); with no `UNIQUE` constraint the store happily accepted **two** issues with `identifier: "ENG-11"` (confirmed: ENG-11 ×2 among 17 issues), and with no `next_val()` the client computes `max(N)+1` over the rows it has loaded — which is racy under concurrency and simply wrong when a filter is active (it maxes over the filtered subset). A unique-field constraint + a server-side sequence/auto-increment would fix both the duplication and the ordering.

## 12. No collation control on sort (byte-order only)
*Surfaced by: Todo (sort by title).*
`sort=<field>` orders by the raw stored value — for strings that's byte/ASCII order, so `"Zebra"` sorts before `"apple"` (uppercase before lowercase) and there's no locale/case-insensitive option. The Todo app has to re-sort the fetched page client-side with `localeCompare` to get a natural A–Z. A per-sort collation flag (case-insensitive / locale-aware) would let the server return the right order (and keep it correct across pagination, which the client-side re-sort cannot).

---

# Architectural gaps for a production rebuild (LinkedIn / Notion / Figma)

A forward-looking analysis: *if you rebuilt the real product*, what infrastructure
would it need, and where does MorphDB fall short. (One principal-architect pass per
product, deduped below.) Findings #1–8 above were hit empirically while building the
toy clones; the items here are the categorical systems a real rebuild requires.

**The honest verdict.** MorphDB is a genuinely good fit for the *system-of-record,
prototype* layer — schema-fluid typed entities, bidirectional relations, indexed
filtering, relation-include hydration, offset pagination over SQLite/Postgres. That
covers maybe **10–20%** of any of these products (modeling + CRUD of profiles/posts/
jobs, the block tree, the scene-graph metadata). Every *load-bearing* system that
makes them what they are sits outside MorphDB. A real build keeps MorphDB (if at all)
as one small CRUD store among a dozen systems: auth, search, object storage+CDN, a
realtime/collab layer, a fan-out/streaming pipeline, an OLAP/aggregation store, and a
transactional core. The gaps, grouped:

### A. Identity & access — the first cliff
- **Authentication & identity** — *blocker (all)*. No login/sessions/OAuth/SSO/MFA/recovery; the server literally cannot tell *which user* is calling (`X-App-Key` is a tenant namespace, not identity). → needs an identity provider + request→user binding. *(deepens #8)*
- **Authorization, permissions, ACLs & sharing** — *blocker (all)*. No per-object/row/field access control, no owner/viewer concept, no roles/teams/orgs, no share links or inherited+overridden page ACLs. Any key-holder reads/writes everything. → needs a policy engine (ReBAC/ABAC) the backend can enforce.

### B. Realtime & collaboration — kills Figma/Notion as-is
- **Realtime transport** (WebSocket/SSE, pub/sub) — *blocker (all)*. Polling-only; two tabs never see each other live. *(= #5)*
- **Conflict-free concurrent editing** (CRDT/OT) + a **per-document authoritative server** + **durable op-log/WAL** — *blocker (Figma, Notion)*. Read-modify-write loses edits; there's no op ordering, merge, or authoritative in-memory document.
- **Presence & live cursors/selections** — *major (Figma, Notion)*.
- **Offline / local-first sync** with reconnect reconciliation — *major (Notion, Figma)*.

### C. Search & discovery
- **Full-text + typeahead + fuzzy + faceted search** — *blocker (all)*. Only `contains` substring on one indexed field; no relevance, tokenization, typo-tolerance, prefix autocomplete, or facets. → dedicated search engine + indexing pipeline. *(= #6)*
- **Semantic / vector search & embeddings** (ANN) — *major→blocker (LinkedIn PYMK/jobs, Notion AI)*. A `json` field can hold an embedding but it isn't queryable; no vector type or ANN index.
- **Graph traversal** — 2nd/3rd-degree, shortest-path, mutual-connection intersection, degree counts — *major (LinkedIn)*. Relations model edges, but `include` depth ≤4 batched hydration is not graph algorithms.

### D. Feeds, aggregation & derived data
- **Feed generation** — fan-out (write/read), candidate gen, dedup, ML ranking, per-user materialized timelines — *blocker (LinkedIn)*. None of it exists.
- **Aggregation / group-by / count-by / distinct / analytics** — *major (all)*: feed/engagement stats, who-viewed-you, per-status counts, board/calendar grouping. Only a flat `total` per filter. *(= #7)*
- **Rollups & formula / computed properties** — *major (Notion)*. No server-side derived fields.
- **Atomic increment counters** — *major (all)*: likes/reactions race. *(= #3)*
- **Atomic relation deltas** (set add/remove) — *major (all)*: connection membership race. *(= #2)*
- **Multi-object transactions & cross-entity invariants** — *blocker/major (all)*: accept-connection (both sides + notification), apply-to-job, payments. *(= #4)*
- **Idempotency & optimistic concurrency** (compare-and-set / version token) — *major (all)*: dedup double-submits, prevent lost updates.

### E. Storage & media
- **Blob / file / media storage + processing + CDN** — *blocker (all)*. Objects are JSON; you can store a URL but not the bytes. No upload endpoint, transcoding, image resize, thumbnail/export rendering, or edge delivery. → object storage (S3/GCS) + media pipeline + CDN.

### F. Async & integration
- **Background jobs / queues / cron / event pipelines** — *blocker (all)*: fan-out, search indexing, embedding generation, email batches, exports, cleanup.
- **Webhooks / triggers / change-data-capture / change-feed** — *blocker (all)*: keep search/feed/analytics in sync; third-party integration. The store emits no change events.
- **Notifications** — generation, fan-out, rollup ("X and 12 others"), in-app/push/email, read state — *blocker (all)*.
- **Transactional / outbound email** — *major (LinkedIn, Notion)*.

### G. History & governance
- **Version history / snapshots / restore / branching+merge** — *blocker (Figma)*, *major (Notion)*. No history; PATCH overwrites.
- **Audit log / activity / compliance trails** (GDPR, SOC2, moderation evidence) — *major (all)*.
- **Soft delete / trash / TTL purge** — *minor (Notion)*.

### H. Scale, performance & ops
- **Query at scale** — cursor pagination (offset-only today), composite/partial indexes, geospatial (jobs-near-me), deep filters — *major (all)*.
- **Write scalability / sharding / horizontal throughput** — *blocker (all)*. A single serialized writer (or N stateless nodes over one Postgres) won't take these products' write volume or hot partitions.
- **Caching / read replicas / CDN / denormalized read views** — *major (all)*.
- **Rate limiting / quotas / anti-abuse / spam / moderation** — *blocker (LinkedIn)*, *major (others)*.
- **Server-side compute / functions / validation rules** — *major (all)*: semantic validation (hex/range/enum), computed defaults, hooks. *(deepens #1)*
- **Observability / metrics / tracing** — *minor*.

### I. Product/business plumbing
- **Billing / seats / plans / workspace admin** — *major (Notion, LinkedIn)*.
- **i18n / localization** — *minor*.

**Bottom line for MorphDB's positioning:** it's an excellent *schema-fluid CRUD system-of-record for prototypes and small tools* — exactly its stated scope. The clones work because a demo only needs the 10–20% MorphDB nails. The list above is what separates "a convincing single-file demo" from "the real product," and almost none of it is expressible against the generic object API. The most reusable primitives MorphDB could add to move the needle, in rough priority: **(1) identity + per-object authorization**, **(2) a change-feed/subscription** (unlocks realtime, webhooks, cache/search sync), **(3) atomic ops** (increment, relation delta, compare-and-set, small transactions), **(4) aggregation/group-by**, and **(5) blob storage**.
