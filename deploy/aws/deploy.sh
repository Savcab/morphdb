#!/usr/bin/env bash
# Build + deploy MorphDB to AWS Lambda behind a public Function URL.
# Idempotent: first run creates the function + URL; later runs update the code.
#
#   # the DB target (a Postgres URL, e.g. Neon's POOLED connection string):
#   export MORPHDB_DATABASE_URL='postgresql://user:pw@host-pooler.../db?sslmode=require'
#   #   ...or write it to ~/.morphdb_neon.url (kept out of shell history / git)
#   ./deploy.sh
#
# Prereqs: run ./setup-iam.sh once; have python3 + the aws CLI + zip installed.
#
# WARNING: the Function URL has NO authentication — anyone with the URL can
# read/write/register apps. Only point it at a database you can expose.
set -euo pipefail

PROFILE="${MORPHDB_DEPLOY_PROFILE:-morphdb}"
REGION="${MORPHDB_AWS_REGION:-us-west-1}"
FN="${MORPHDB_FN_NAME:-morphdb-api}"
ROLE="${MORPHDB_EXEC_ROLE:-morphdb-lambda-exec}"
RUNTIME=python3.12
PYVER=3.12
ARCH=arm64
WHEEL_PLATFORM=manylinux2014_aarch64

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$(mktemp -d)"; ZIP="$(mktemp -u).zip"
trap 'rm -rf "$BUILD" "$ZIP"' EXIT

# load a local, gitignored env file if present (deploy/aws/.env)
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi

# --- resolve the DB target (never echoed) ------------------------------------
DBURL="${MORPHDB_DATABASE_URL:-}"
if [ -z "$DBURL" ] && [ -f "$HOME/.morphdb_neon.url" ]; then
  DBURL="$(cat "$HOME/.morphdb_neon.url")"
fi
if [ -z "$DBURL" ]; then
  echo "ERROR: set MORPHDB_DATABASE_URL or write the URL to ~/.morphdb_neon.url" >&2
  exit 1
fi
case "$DBURL" in
  postgres://*|postgresql://*) : ;;
  *) echo "ERROR: MORPHDB_DATABASE_URL must be a postgres:// URL for a hosted DB." >&2; exit 1 ;;
esac

ACCT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
ROLE_ARN="arn:aws:iam::${ACCT}:role/${ROLE}"
if ! aws iam get-role --role-name "$ROLE" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "ERROR: execution role '$ROLE' not found. Run ./setup-iam.sh first." >&2
  exit 1
fi

# --- build the deployment package --------------------------------------------
echo "building package (morphdb + psycopg for $ARCH/$RUNTIME)..."
cp -R "$REPO_ROOT/morphdb" "$BUILD/morphdb"
cp "$HERE/lambda_function.py" "$BUILD/lambda_function.py"
PIP=(python3 -m pip); "${PIP[@]}" --version >/dev/null 2>&1 || PIP=(pip3)
"${PIP[@]}" install --quiet --target "$BUILD" --only-binary=:all: \
  --platform "$WHEEL_PLATFORM" --implementation cp --python-version "$PYVER" --abi "cp${PYVER//./}" \
  'psycopg[binary]>=3.1'
find "$BUILD" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name 'tests' -prune -exec rm -rf {} + 2>/dev/null || true
( cd "$BUILD" && zip -qr "$ZIP" . )
echo "package: $(du -h "$ZIP" | cut -f1)"

# --- create or update the function -------------------------------------------
if aws lambda get-function --function-name "$FN" --region "$REGION" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "updating function code..."
  aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$ZIP" \
    --region "$REGION" --profile "$PROFILE" >/dev/null
else
  echo "creating function $FN..."
  aws lambda create-function --function-name "$FN" \
    --runtime "$RUNTIME" --architectures "$ARCH" --handler lambda_function.handler \
    --role "$ROLE_ARN" --timeout 30 --memory-size 512 \
    --zip-file "fileb://$ZIP" --region "$REGION" --profile "$PROFILE" >/dev/null
fi
aws lambda wait function-active-v2 --function-name "$FN" --region "$REGION" --profile "$PROFILE" 2>/dev/null \
  || aws lambda wait function-updated --function-name "$FN" --region "$REGION" --profile "$PROFILE"

# --- env (DB URL passed via a 0600 temp file, never on the command line) ------
ENVJSON="$(mktemp)"; trap 'rm -rf "$BUILD" "$ZIP" "$ENVJSON"' EXIT
( umask 077; python3 - "$DBURL" >"$ENVJSON" <<'PY'
import json, sys
print(json.dumps({"Variables": {"MORPHDB_DATABASE_URL": sys.argv[1], "MORPHDB_QUIET": "1"}}))
PY
)
aws lambda update-function-configuration --function-name "$FN" \
  --environment "file://$ENVJSON" --region "$REGION" --profile "$PROFILE" >/dev/null
aws lambda wait function-updated --function-name "$FN" --region "$REGION" --profile "$PROFILE"

# --- public Function URL ------------------------------------------------------
if ! aws lambda get-function-url-config --function-name "$FN" --region "$REGION" --profile "$PROFILE" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$FN" --auth-type NONE \
    --cors '{"AllowOrigins":["*"],"AllowMethods":["*"],"AllowHeaders":["*"]}' \
    --region "$REGION" --profile "$PROFILE" >/dev/null
  aws lambda add-permission --function-name "$FN" --statement-id public-url \
    --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE \
    --region "$REGION" --profile "$PROFILE" >/dev/null 2>&1 || true
fi
URL="$(aws lambda get-function-url-config --function-name "$FN" \
  --query FunctionUrl --output text --region "$REGION" --profile "$PROFILE")"

echo
echo "deployed: $FN ($ARCH, $RUNTIME) in $REGION"
echo "URL: $URL"
echo
echo "point local clients at it:"
echo "  export MORPHDB_HOST='${URL%/}'"
echo "smoke it:"
echo "  curl -s ${URL%/}/health"
