# DynamoDB Table Creation Policy

Date: 2026-06-27

Status: Accepted

## Decision

MorphDB should verify an existing DynamoDB table by default, and support
explicit opt-in table creation for local development or setup.

## Direction

- Verify an existing table by default.
- Allow explicit opt-in table creation, for example with `create_table=true`.
- Do not implicitly create production tables.
- Never mutate billing mode, IAM, backups, or other production table settings on
  an existing table.
- When MorphDB creates a table, use the expected MorphDB key/index layout.

## Rationale

On-demand creation is valuable for smooth vibe-coding and local setup. Production
deployments should usually manage DynamoDB tables through Terraform,
CloudFormation, CDK, Pulumi, the AWS console, or another infrastructure workflow.

This mirrors the current split between SQLite convenience and managed-database
production discipline.
