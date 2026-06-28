# DynamoDB Multiple Filters

Date: 2026-06-28

Status: Accepted

## Decision

Preserve MorphDB's existing multiple-filter behavior for the initial DynamoDB
backend.

The DynamoDB backend should support combined filters with the same public
semantics as SQLite/Postgres, including field filters and relation filters joined
with `AND`.

## DynamoDB Caveat

DynamoDB does not combine arbitrary independent indexes the way a relational
database planner can. A DynamoDB `Query` is most efficient when a request maps to
one table/index partition and optional sort-key condition.

Multiple MorphDB filters may therefore require candidate reads, index-set
intersection, or application-side post-filtering.

## Query Planning Direction

Implement a simple best-access-path planner:

- Prefer the most selective efficient access path as the base query.
- Use relation equality filters when they provide a narrow relation-index lookup.
- Use indexed scalar field equality/range filters when generic field-index records
  can provide a useful candidate set.
- Use app/type object listing as the fallback base path.
- Apply remaining filters by index-set intersection or application-side
  filtering to preserve correctness.
- Preserve exact `total`, even when that requires walking the candidate set.

## Documentation And Future Work

Backend performance documentation should tell agents that a single indexed filter
is usually more efficient than many combined filters on DynamoDB.

Future performance-optimized mode may warn on, reject, or require opt-in for
multi-filter patterns that require broad reads or expensive post-filtering.
