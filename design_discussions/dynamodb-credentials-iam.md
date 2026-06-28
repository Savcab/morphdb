# DynamoDB Credentials And IAM

Date: 2026-06-27

Status: Accepted

## Decision

Do not read a database password or database secret for DynamoDB. Use AWS IAM and
the boto3 credential chain.

## Direction

Local development can use:

- `AWS_PROFILE`
- `~/.aws/credentials`
- AWS SSO
- environment credentials

AWS runtimes can use:

- Lambda execution roles
- ECS task roles
- EC2 instance roles
- CI/OIDC role assumption

The MorphDB target URL should stay non-secret:

```bash
MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
```

## AWS Lambda Note

The current AWS deploy tooling creates a Lambda execution role for MorphDB. For
DynamoDB, that runtime role should receive least-privilege DynamoDB permissions
for the configured table and indexes. `CreateTable` should only be granted in
environments where explicit table creation is allowed.
