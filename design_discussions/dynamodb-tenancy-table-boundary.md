# DynamoDB Tenancy And Table Boundary

Date: 2026-06-27

Status: Accepted

## Context

MorphDB currently treats an `app` as the tenant boundary. Each app has its own
schema, objects, relations, and derived indexes. Requests select the app with the
`X-App-Key` header, and the storage layer scopes all data to that app key.

For a company-hosted internal MorphDB deployment, the company is the operator of
the deployment. Each vibe-coded application created by users inside the company
should be represented as a separate MorphDB app.

## Current Direction

Use one DynamoDB table per MorphDB deployment or environment, with many isolated
MorphDB apps inside that table.

Example:

```text
Company MorphDB deployment
  DynamoDB table: morphdb-prod
    MorphDB app: alice-crm
    MorphDB app: design-review-tracker
    MorphDB app: hackathon-inventory
    MorphDB app: support-dashboard
```

This matches MorphDB's existing SQLite/Postgres model:

- one storage target backs a MorphDB deployment
- many apps live inside that storage target
- the MorphDB app key is the tenant namespace
- app data should not leak across app keys

In DynamoDB terms, this means "one table for multiple apps," not one table per
app. DynamoDB does not have a database instance in the same sense as Postgres or
RDS; the practical boundary here is the DynamoDB table.

## Why This Is The Default

One table per deployment keeps the developer/operator experience simple for the
main MorphDB use case: many small vibe-coded apps with modest traffic.

Benefits:

- fewer AWS resources to create and manage
- shared table/index layout for all apps
- easier local and internal company deployments
- matches the current MorphDB multi-tenant model
- avoids creating infrastructure for every prototype app

## Future Option: One Table Per App

We may support one DynamoDB table per MorphDB app in the future when stronger
isolation is worth the extra operational overhead.

Reasons to choose one table per app could include:

- stronger AWS-level isolation
- separate IAM policies per app
- clearer per-app billing visibility
- independent backup and restore
- smaller blast radius for accidental deletes or table-level changes
- app-specific throughput or capacity tuning

This should be treated as an advanced deployment mode, not the default. The
default remains one DynamoDB table per MorphDB deployment, with the app key as
the logical tenant boundary.
