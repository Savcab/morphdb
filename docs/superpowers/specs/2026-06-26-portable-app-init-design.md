# Portable app schema + `morphdb init`

**Date:** 2026-06-26
**Status:** approved, pre-implementation

## Goal

Let anyone ship a MorphDB-backed website whose data model travels with the repo
as a single root JSON file, and stand that app up on their own MorphDB backend
with one idempotent command. Building an app online → commit
`morphdb.schema.json` → someone else clones it, points at their own MorphDB, runs
one command, and the app comes up against a clean (or merged) backend.

## Convention

`morphdb.schema.json` at the website repo root is the app's portable definition:
app key + every type's fields + relations. Produced by the existing
`morphdb export-schema <app>` (unchanged), e.g.:

```
morphdb export-schema my-site > morphdb.schema.json
```

The file format is the current export payload: `morphdb_schema_version`, `app`,
`types[]` (each `{name, fields, relations}`). No format change.

## New command: `morphdb init [file]`

The front door for standing an app up from its schema file.

- **Argument:** optional `file`, default `./morphdb.schema.json`. If the default
  is used and the file is absent, exit with a clear message naming the expected
  path and `export-schema`.
- **Validation:** reuse reconstruct's checks — readable JSON, dict with `app` and
  `types`. Bad file → exit with a specific error.
- **Targets** the backend at `--url` / `$MORPHDB_HOST` (same as every other
  command). App key comes from the file.

### Behavior

| State | Action | Report |
|-------|--------|--------|
| App missing | Create app, apply schema two-pass (all types' fields, then relations so relation targets exist). | `{"app": X, "status": "created", "types": [...]}` |
| App exists, no `--reset` | **Never deletes.** Print notice to stderr: `app 'X' already exists — merging schema additively; existing data kept.` Apply schema as a merge (`merge: true` for fields and relations). Idempotent: already-matching → effective no-op. | `{"app": X, "status": "merged", "types": [...]}` |
| App exists, `--reset` | Destructive clean rebuild. Interactive: confirm `Overwrite app 'X'? Deletes its schema and ALL objects. [y/N]`. Non-tty: require the flag already given (it is), proceed without hanging. Delete app, recreate, apply schema. | `{"app": X, "status": "reset", "types": [...]}` |

The clash case: re-running `init` onto an existing key **merges and warns** — it
does not wipe. Wiping is only ever `--reset`.

### Merge semantics

Fields and relations are applied with `merge: true` (the existing `_put_type`
merge path). Additive only: new fields/relations are added; existing ones whose
definitions match are no-ops. **Out of scope:** detecting/removing fields that
exist in the backend but not in the file (no destructive diff on merge). `--reset`
is the way to get an exact match to the file.

## Removed: `reconstruct-schema`

Dropped entirely and replaced by `init`. It is one week old (shipped in v0.3.0)
with no external dependents. `init` is strictly more capable: file default,
idempotent merge, safe clash handling, opt-in `--reset`. `--force` is gone; its
clean-rebuild behavior moves to `--reset`.

`export-schema` is unchanged.

## Implementation notes

- `morphdb/cli/schema.py`: replace `cmd_reconstruct_schema` with `cmd_init`;
  the destructive path (delete + recreate) becomes the `--reset` branch; add the
  create-or-merge default branch; add the file default. Update `add_commands`
  parser wiring (`init`, positional `file` with default, `--reset` flag) and drop
  the `reconstruct-schema` subparser.
- `morphdb/cli/main.py`: update the usage block (drop `reconstruct-schema`, add
  `init`).
- The two-pass apply (fields then relations) already exists in
  `cmd_reconstruct_schema`; the create-and-apply helper is shared by both the
  `created` and `reset` paths.

## Docs to update

- `README.md` schema-sharing section (~179): `init` flow + `morphdb.schema.json`
  root convention.
- `site/index.html` (~441): the two-line snippet → `export-schema` then `init`.
- `morphdb/skill/SKILL.md` "Sharing a schema across instances" section (~258) and
  the breadcrumb block: document `init`, the root-file convention, and `--reset`.

## Tests (`tests/test_cli.py`)

Replace the two reconstruct tests:

1. **export → init roundtrip on a fresh app** → `status: created`, schema matches.
2. **init onto existing app merges, keeps data** — seed an object, re-run `init`,
   assert `status: merged`, the object still present, schema intact. (Verifies the
   no-hard-delete guarantee — the core of the request.)
3. **`init --reset` on existing app** → `status: reset`, app rebuilt, prior object
   gone.
4. **default file** — `init` with no arg reads `./morphdb.schema.json` from cwd
   (chdir a tempdir holding the file).
5. **missing default file** → exits with a helpful message.
```
