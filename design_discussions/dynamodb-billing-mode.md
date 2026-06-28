# DynamoDB Billing Mode

Date: 2026-06-27

Status: Accepted

## Decision

When MorphDB creates a DynamoDB table, default to DynamoDB on-demand billing.
When a table already exists, leave its billing mode unchanged.

## Rationale

The expected DynamoDB use case is many small, spiky prototype or internal apps.
On-demand billing avoids capacity planning and fits variable traffic.

Provisioned capacity remains an advanced operator choice for predictable,
high-volume workloads. Operators who want provisioned capacity should create and
manage the table outside MorphDB, then point MorphDB at it.
