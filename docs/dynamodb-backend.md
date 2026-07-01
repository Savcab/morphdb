# DynamoDB Backend

MorphDB can use DynamoDB as its durable store while keeping the same HTTP API as
SQLite and PostgreSQL.

## Install

```bash
pip install 'morphdb[dynamodb]'
```

The extra installs `boto3`. The default SQLite install remains dependency-free.

## Target URL

Use `MORPHDB_DATABASE_URL` or `--db`:

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
morphdb start
```

Optional query parameters:

| Parameter | Purpose |
| --- | --- |
| `region` | AWS region for the boto3 session. |
| `profile` | Named AWS profile for local development. |
| `endpoint_url` | DynamoDB Local or LocalStack endpoint. |
| `create_table=true` | Create the table if missing. Prefer omitting this in production. |

DynamoDB credentials come from the normal boto3 chain: environment variables,
shared config profiles, web identity, EC2/ECS/Lambda roles, and similar runtime
providers. Do not put an AWS secret or database password in the URL.

## Table Shape

MorphDB uses one DynamoDB table per deployment/environment and stores many
MorphDB apps inside it. The table has:

| Key/index | Attributes |
| --- | --- |
| Table primary key | `pk` / `sk` |
| GSI `by_app` | `gsi1pk` / `gsi1sk`, keys-only |
| GSI `by_type_updated` | `gsi2pk` / `gsi2sk`, all projection |

When MorphDB creates the table, it uses on-demand billing. Production
deployments should usually create the table and IAM policy through Terraform,
CloudFormation, CDK, or equivalent infrastructure tooling.

## API Parity

DynamoDB supports the same public MorphDB behavior:

- schemas, field defaults, lazy retyping
- object CRUD
- relation reads/writes/includes
- field filters on fields marked `"index": true`
- relation filters
- exact `total`
- `limit`/`offset` pagination
- sorting by indexed fields and system fields

## Performance Notes

MorphDB prioritizes correctness and API parity for the initial DynamoDB backend.
Some valid MorphDB requests can require DynamoDB to read more candidate items,
then filter/sort/page in the MorphDB process.

Prefer:

- selective indexed filters
- relation equality filters such as `?assignee=<guid>`
- small `limit` values
- small offsets or "load more" UI
- filtering before sorting when possible

Use care with:

- large `offset` values
- exact totals on broad filtered reads
- `__contains` substring filters
- negative relation filters such as `rel__ne=<guid>`
- many independent filters that do not share one DynamoDB access path
- sorting broad result sets by a field

These patterns still return correct MorphDB results. They are just less natural
for DynamoDB than for SQLite/PostgreSQL. A future performance mode may add cursor
pagination or opt out of exact totals for apps that need lower read cost.

## Local Development

DynamoDB Local example:

```bash
morphdb start --db 'dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true'
```

The dashboard works against DynamoDB and shows logical MorphDB views. The raw SQL
table explorer remains SQLite/PostgreSQL-specific.

## AWS Lambda Deploy

The AWS deploy helper can run MorphDB as a Lambda Function URL backed by
DynamoDB:

```bash
cd deploy/aws
./setup-iam.sh
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
./deploy.sh
```

For quick prototypes, append `&create_table=true` so MorphDB creates the table
with on-demand billing on first cold start. Production deployments should usually
create the table outside MorphDB and omit `create_table=true`.

Do not include `endpoint_url` in hosted Lambda deployments. That parameter is
only for DynamoDB Local/LocalStack.
