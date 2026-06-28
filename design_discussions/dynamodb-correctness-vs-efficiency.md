# DynamoDB Correctness Versus Efficiency

Date: 2026-06-28

Status: Accepted

## Decision

The initial DynamoDB backend should prioritize MorphDB API correctness over raw
DynamoDB efficiency.

Where DynamoDB provides an efficient access path, the backend should use it. When
an operation does not map cleanly to DynamoDB, the backend should still return
the correct MorphDB result and document the cost.

## Direction

Correctness requirements include:

- exact list results
- exact `total`
- `limit`/`offset` behavior
- sorting parity
- multiple-filter parity
- relation-filter parity
- `contains` parity
- lazy default-value semantics

Efficiency work should focus on common paths:

- object lookup by guid
- app/type object listing
- positive relation lookup
- simple indexed field equality/range lookup
- batch fetches after index lookup

## Documentation

Backend performance documentation should identify operations that may require:

- broad reads
- scans
- index-set intersection
- application-side sorting
- relation set subtraction
- walking DynamoDB pages to emulate offset or exact totals

Future performance-optimized mode may explicitly trade parity features for lower
latency or lower DynamoDB read cost, but that should be opt-in and documented.
