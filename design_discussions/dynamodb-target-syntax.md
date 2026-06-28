# DynamoDB Target Syntax

Date: 2026-06-27

Status: Accepted

## Decision

Add a `dynamodb://` storage target URL.

Example:

```text
dynamodb://morphdb-prod?region=us-west-2
```

Supported query parameters:

- `region`
- `endpoint_url`
- `profile`
- `create_table`

Examples:

```text
dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true
dynamodb://morphdb-prod?region=us-west-2
dynamodb://morphdb-dev?region=us-west-2&profile=my-dev-profile
```

## Credential Rule

Do not embed AWS access keys or secrets in the MorphDB URL. Credentials should be
resolved through AWS IAM and the boto3 credential chain.
