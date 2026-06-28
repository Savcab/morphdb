# DynamoDB Implementation Strategy

Date: 2026-06-28

Status: Accepted

## Decision

Use a big-bang refactor to introduce the logical MorphDB storage interface needed
for DynamoDB.

Rather than gradually threading DynamoDB-specific branches through the existing
SQL-shaped modules, refactor the storage boundary in one coordinated pass.

## Rationale

The current backend seam is SQL-shaped. DynamoDB does not fit that seam cleanly.
Attempting a long strangler migration could leave the codebase with mixed SQL and
DynamoDB assumptions for too long.

A coordinated refactor can establish the correct abstraction boundary first:

```text
MorphDB API / domain behavior
  -> logical storage interface
    -> SQLite/Postgres SQL implementation
    -> DynamoDB-native implementation
```

## Guardrails

The refactor must be behavior-led:

- preserve SQLite behavior
- preserve Postgres behavior
- keep public MorphDB API parity
- run existing tests frequently
- add contract tests for the logical storage interface
- avoid DynamoDB-specific assumptions leaking into domain/API code

## Rejected Alternatives

Avoid adding scattered `if backend == "dynamodb"` branches across domain modules.
That would make the behavior harder to reason about and would undermine the goal
of a clean backend-native implementation behind one MorphDB API.
