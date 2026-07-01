# Host MorphDB on AWS Lambda + Postgres or DynamoDB

Run MorphDB as a public HTTP endpoint backed by external durable storage,
so your local coding agents and frontends can reach it from anywhere. The
compute is a single AWS Lambda behind a **Function URL**; the data lives in
either managed Postgres or one DynamoDB table.

This is **additive deploy tooling** — it imports and reuses the existing
`morphdb` package unchanged. The adapter (`lambda_function.py`) is the same thin
HTTP shim `morphdb.server` already uses, re-expressed for a Function URL.

```
                MORPHDB_HOST=https://…lambda-url…        MORPHDB_DATABASE_URL=postgres://…
  your agents / frontend  ───────────────────────▶  Lambda (morphdb)  ──────────────────▶  Postgres

                MORPHDB_HOST=https://…lambda-url…        MORPHDB_DATABASE_URL=dynamodb://…
  your agents / frontend  ───────────────────────▶  Lambda (morphdb)  ──────────────────▶  DynamoDB
```

## ⚠️ No authentication

The Function URL is **public and unauthenticated** — anyone who has the URL can
read, write, and register apps. Keep the URL private and only point it at a
database you are comfortable exposing for testing. (A backward-compatible
`MORPHDB_TOKEN` bearer gate can be added later; ask if you want it.)

## Prerequisites

- AWS CLI v2, `python3`, and `zip` installed.
- A storage target:
  - Postgres URL. For Neon:
    1. Sign up at <https://neon.tech> (free).
    2. Create a project (region: pick the one closest to your Lambda region).
    3. Copy the **Pooled** connection string (`…-pooler.…/db?sslmode=require`) —
       pooled is important because Lambda opens many short-lived connections.
  - Or DynamoDB URL, e.g. `dynamodb://morphdb-prod?region=us-west-2`.
    For production, create the table with IaC/console first; for quick tests,
    add `&create_table=true` and let MorphDB create the table on first cold
    start.

## Setup (once)

```bash
cd deploy/aws
./setup-iam.sh         # creates the Lambda role + a scoped 'morphdb' deploy user/profile
```

If you already ran `setup-iam.sh` before DynamoDB deploy support existed, run it
again. It updates the deploy user so `deploy.sh` can attach a scoped DynamoDB
runtime policy to the Lambda execution role.

## Deploy

Postgres:

```bash
# stash the DB URL out of shell history + git:
(umask 077; printf '%s' 'postgresql://…-pooler.…/neondb?sslmode=require' > ~/.morphdb_neon.url)

./deploy.sh            # builds the zip, creates/updates the function, prints the URL
```

DynamoDB:

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
./deploy.sh

# or for a short-lived prototype table:
export MORPHDB_DATABASE_URL='dynamodb://morphdb-dev?region=us-west-2&create_table=true'
./deploy.sh
```

`deploy.sh` is idempotent — re-run it any time to push code changes.

For the public Function URL, `deploy.sh` configures `AuthType=NONE` and the two
resource-policy permissions AWS requires for anonymous URL invocations.

For DynamoDB, `deploy.sh` packages `boto3` with the Lambda bundle and attaches a
least-privilege inline policy named `morphdb-dynamodb-runtime` to the execution
role for the configured table and its indexes. Do not include `endpoint_url` in
a hosted Lambda deployment; that option is only for DynamoDB Local/LocalStack.

## Point your local tools at it

```bash
export MORPHDB_HOST='https://<your-id>.lambda-url.us-west-1.on.aws'
```

Everything that already honors `MORPHDB_HOST` now hits the hosted backend with
no other change: the MorphDB skill, the `morphdb` schema CLI, and generated
frontends (which read `window.MORPHDB_HOST`). Do **not** also run a local `morphdb start`;
when `MORPHDB_HOST` is set the clients talk to the hosted server directly.

## Inspect the data

The admin dashboard reads storage directly, so run it locally pointed at the
same backend (no hosted dashboard is deployed):

```bash
export MORPHDB_DATABASE_URL="$(cat ~/.morphdb_neon.url)"
morphdb dashboard       # opens the read-only dashboard against Neon

export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
morphdb dashboard       # opens the logical dashboard against DynamoDB
```

For DynamoDB, the dashboard shows MorphDB's logical tables. Use the AWS Console
or `aws dynamodb scan` when you need the raw physical single-table items.

## Update / tear down

```bash
./deploy.sh             # redeploy after code changes
./teardown.sh           # remove the function + public URL (database untouched)
./teardown.sh --iam     # also remove the deploy user + role
```

## Notes

- **Region/runtime:** defaults to `us-west-1`, `python3.12`, `arm64`. Override
  with `MORPHDB_AWS_REGION`, or edit `deploy.sh`.
- **Schema:** created automatically on the first (cold-start) request.
- **Cost:** Lambda Function URL stays within the always-free tier at personal
  scale. Neon has a free tier. AWS RDS and DynamoDB are billable AWS services;
  DynamoDB test usage is usually tiny, but set billing alerts in personal
  accounts.
