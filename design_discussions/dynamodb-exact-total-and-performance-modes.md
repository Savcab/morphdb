# DynamoDB Exact Totals And Performance Modes

Date: 2026-06-28

Status: Accepted

## Decision

The initial DynamoDB backend should preserve MorphDB's existing exact `total`
behavior on list endpoints.

This keeps API parity with SQLite/Postgres and avoids making DynamoDB a narrower
behavioral backend.

## Caveat

Exact totals can be expensive on DynamoDB. For broad queries, filtered queries,
negative filters, `contains`, or multi-filter queries, the backend may need to
walk many matching or candidate items to compute the exact count.

This is acceptable for the initial backend because MorphDB's primary target is
small-to-medium vibe-coded apps and prototypes, but it must be documented
clearly.

## Future Performance Mode

Future work should consider an explicit performance-optimized mode, such as:

```text
performance_optimized=true
```

or a similarly named API/config option.

In performance-optimized mode, MorphDB could opt out of expensive parity features
for backends where they do not map naturally to efficient access patterns. For
DynamoDB, this could mean:

- omit exact `total`
- prefer cursor pagination over offset traversal
- reject or warn on scan-heavy filters
- expose `has_more` / `next_cursor` instead of exact page counts

This should be an explicit mode, not an accidental backend difference.

## Agent-Readable Backend Performance Documentation

Each storage backend should document endpoint-level performance characteristics
in a format AI agents can read before generating application code.

Possible forms:

- backend-specific Markdown files, such as `docs/backends/dynamodb.md`
- endpoint docstrings that note backend-specific performance behavior
- generated API documentation that includes backend caveats per endpoint

For DynamoDB, the docs should clearly identify which endpoint patterns are
efficient and which may be expensive.

Examples:

- Efficient: get object by guid.
- Efficient: list first page by app/type in natural order.
- Potentially expensive: exact `total` for broad filtered queries.
- Potentially expensive: large offset pagination.
- Potentially expensive: `contains`, `ne`, `exists=false`, and some relation
  filters.

The goal is not only to inform humans, but to steer coding agents toward better
UX and data-access choices when the configured backend is DynamoDB.
