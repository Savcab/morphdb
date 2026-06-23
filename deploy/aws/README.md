# Host MorphDB on AWS Lambda + a Postgres database

Run MorphDB as a public HTTP endpoint backed by an external Postgres database,
so your local coding agents and frontends can reach it from anywhere. The
compute is a single AWS Lambda behind a **Function URL**; the data lives in a
managed Postgres (this guide uses **Neon**'s free tier — always free, no card —
but any `postgres://` URL works, including AWS RDS).

This is **additive deploy tooling** — it imports and reuses the existing
`morphdb` package unchanged. The adapter (`lambda_function.py`) is the same thin
HTTP shim `morphdb.server` already uses, re-expressed for a Function URL.

```
                MORPHDB_HOST=https://…lambda-url…        MORPHDB_DATABASE_URL=postgres://…
  your agents / frontend  ───────────────────────▶  Lambda (morphdb)  ──────────────────▶  Neon Postgres
```

## ⚠️ No authentication

The Function URL is **public and unauthenticated** — anyone who has the URL can
read, write, and register apps. Keep the URL private and only point it at a
database you are comfortable exposing for testing. (A backward-compatible
`MORPHDB_TOKEN` bearer gate can be added later; ask if you want it.)

## Prerequisites

- AWS CLI v2, `python3`, and `zip` installed.
- A Postgres database URL. For Neon:
  1. Sign up at <https://neon.tech> (free).
  2. Create a project (region: pick the one closest to your Lambda region).
  3. Copy the **Pooled** connection string (`…-pooler.…/db?sslmode=require`) —
     pooled is important because Lambda opens many short-lived connections.

## Setup (once)

```bash
cd deploy/aws
./setup-iam.sh         # creates the Lambda role + a scoped 'morphdb' deploy user/profile
```

## Deploy

```bash
# stash the DB URL out of shell history + git:
(umask 077; printf '%s' 'postgresql://…-pooler.…/neondb?sslmode=require' > ~/.morphdb_neon.url)

./deploy.sh            # builds the zip, creates/updates the function, prints the URL
```

`deploy.sh` is idempotent — re-run it any time to push code changes.

## Point your local tools at it

```bash
export MORPHDB_HOST='https://<your-id>.lambda-url.us-west-1.on.aws'
```

Everything that already honors `MORPHDB_HOST` now hits the hosted backend with
no other change: the MorphDB skill, the `morphdb` MCP, and generated frontends
(which read `window.MORPHDB_HOST`). Do **not** also run a local `morphdb start`;
when `MORPHDB_HOST` is set the clients talk to the hosted server directly.

## Inspect the data

The admin dashboard reads the database directly, so run it locally pointed at
the same Postgres (no hosted dashboard is deployed):

```bash
export MORPHDB_DATABASE_URL="$(cat ~/.morphdb_neon.url)"
morphdb dashboard       # opens the read-only dashboard against Neon
```

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
  scale; Neon's free tier is $0. (AWS RDS, if you use it instead of Neon, is
  only free for ~12 months and then bills monthly.)
