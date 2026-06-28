# DynamoDB Scale Target

Date: 2026-06-28

Status: Accepted

## Decision

The initial DynamoDB backend should target many low-to-moderate-traffic MorphDB
apps in one DynamoDB table.

Each MorphDB app may have its own schema, and many apps may be active
concurrently. However, the expected use case is vibe-coding/prototyping rather
than high-traffic production SaaS workloads per app.

## Design Implications

- Use one table per MorphDB deployment by default.
- Expect many app keys inside that table.
- Avoid key designs that put all deployment traffic into one partition.
- Avoid key designs that put all traffic for a busy app into one app-only hot
  partition.
- Include app/type/object/index dimensions in access paths where useful.
- Use on-demand billing by default for MorphDB-created tables.
- Optimize common prototype workloads first.

## Future Options

For heavier workloads, stronger isolation, or clearer per-app operational
boundaries, MorphDB may support one DynamoDB table per app in the future.

Additional production tuning may include more specialized access-pattern indexes,
cursor pagination, performance-optimized mode, or backend-specific guidance that
nudges agents away from expensive query patterns.
