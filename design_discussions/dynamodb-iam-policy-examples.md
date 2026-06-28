# DynamoDB IAM Policy Examples

Date: 2026-06-28

Status: Accepted

## Decision

DynamoDB documentation should include IAM policy examples for MorphDB runtime
access and separate dev/setup table-creation permissions.

## Production Runtime Policy

For a production deployment pointed at an existing table, the Lambda execution
role or runtime identity should receive permissions scoped to the configured
DynamoDB table and its indexes.

Expected actions include:

```text
dynamodb:DescribeTable
dynamodb:GetItem
dynamodb:PutItem
dynamodb:UpdateItem
dynamodb:DeleteItem
dynamodb:Query
dynamodb:Scan
dynamodb:BatchGetItem
dynamodb:BatchWriteItem
dynamodb:TransactWriteItems
```

Expected resources:

```text
arn:aws:dynamodb:<region>:<account>:table/<table>
arn:aws:dynamodb:<region>:<account>:table/<table>/index/*
```

## Dev/Setup Additions

Only environments that explicitly allow table creation, such as local/dev setup
with `create_table=true`, should add table-management permissions such as:

```text
dynamodb:CreateTable
dynamodb:UpdateTable
```

`dynamodb:DeleteTable` should not be included by default.

## Deployment Tooling

AWS deploy docs or scripts may help attach a scoped DynamoDB runtime policy when
a DynamoDB target is configured. They should avoid broad AWS managed policies for
runtime access.
