#!/usr/bin/env bash
# Build + deploy MorphDB to AWS Lambda behind a public Function URL.
# Idempotent: first run creates the function + URL; later runs update the code.
#
#   # the DB target (Postgres or DynamoDB):
#   export MORPHDB_DATABASE_URL='postgresql://user:pw@host-pooler.../db?sslmode=require'
#   export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
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
CLEANUP_FILES=()
cleanup() {
  rm -rf "$BUILD" "$ZIP"
  if [ "${#CLEANUP_FILES[@]}" -gt 0 ]; then
    rm -f "${CLEANUP_FILES[@]}"
  fi
}
trap cleanup EXIT

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

DBINFO="$(python3 - "$DBURL" "$REGION" <<'PY'
import sys
from urllib.parse import parse_qs, urlparse

url = sys.argv[1]
lambda_region = sys.argv[2]
p = urlparse(url)
q = parse_qs(p.query)
scheme = p.scheme
if scheme in {"postgres", "postgresql"}:
    print("postgres\t-\t-\tfalse\t-")
elif scheme == "dynamodb":
    table = p.netloc
    if not table:
        raise SystemExit("ERROR: DynamoDB URL must include a table name.")
    region = (q.get("region") or [lambda_region])[0]
    if not region:
        raise SystemExit("ERROR: DynamoDB deploy needs ?region=... or MORPHDB_AWS_REGION.")
    create_table = (q.get("create_table") or ["false"])[0].lower() in {
        "1", "true", "yes", "on",
    }
    endpoint = (q.get("endpoint_url") or ["-"])[0]
    print(f"dynamodb\t{table}\t{region}\t{str(create_table).lower()}\t{endpoint or '-'}")
else:
    raise SystemExit(
        "ERROR: MORPHDB_DATABASE_URL must be postgresql://... or dynamodb://..."
    )
PY
)"
IFS=$'\t' read -r DB_KIND DDB_TABLE DDB_REGION DDB_CREATE_TABLE DDB_ENDPOINT <<<"$DBINFO"
if [ "$DB_KIND" = "dynamodb" ] && [ "$DDB_ENDPOINT" != "-" ]; then
  echo "ERROR: endpoint_url is for DynamoDB Local/LocalStack and must not be used in Lambda deploys." >&2
  exit 1
fi

ACCT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
ROLE_ARN="arn:aws:iam::${ACCT}:role/${ROLE}"
if ! aws iam get-role --role-name "$ROLE" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "ERROR: execution role '$ROLE' not found. Run ./setup-iam.sh first." >&2
  exit 1
fi
if [ "$DB_KIND" = "postgres" ]; then
  aws iam delete-role-policy --role-name "$ROLE" --policy-name morphdb-dynamodb-runtime \
    --profile "$PROFILE" >/dev/null 2>&1 || true
fi

# --- build the deployment package --------------------------------------------
case "$DB_KIND" in
  postgres)
    echo "building package (morphdb + psycopg for $ARCH/$RUNTIME)..."
    DEPS=('psycopg[binary]>=3.1')
    ;;
  dynamodb)
    echo "building package (morphdb + boto3 for $ARCH/$RUNTIME)..."
    DEPS=('boto3>=1.34')
    ;;
esac
cp -R "$REPO_ROOT/morphdb" "$BUILD/morphdb"
cp "$HERE/lambda_function.py" "$BUILD/lambda_function.py"
PIP=(python3 -m pip); "${PIP[@]}" --version >/dev/null 2>&1 || PIP=(pip3)
"${PIP[@]}" install --quiet --target "$BUILD" --only-binary=:all: \
  --platform "$WHEEL_PLATFORM" --implementation cp --python-version "$PYVER" --abi "cp${PYVER//./}" \
  "${DEPS[@]}"
find "$BUILD" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name 'tests' -prune -exec rm -rf {} + 2>/dev/null || true
( cd "$BUILD" && zip -qr "$ZIP" . )
echo "package: $(du -h "$ZIP" | cut -f1)"

# --- runtime IAM for DynamoDB -------------------------------------------------
if [ "$DB_KIND" = "dynamodb" ]; then
  POLICY="$(mktemp)"; CLEANUP_FILES+=("$POLICY")
  python3 - "$ACCT" "$DDB_REGION" "$DDB_TABLE" "$DDB_CREATE_TABLE" >"$POLICY" <<'PY'
import json
import sys

account, region, table, create_table = sys.argv[1:5]
table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{table}"
actions = [
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:TransactWriteItems",
]
if create_table == "true":
    actions.append("dynamodb:CreateTable")
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": actions,
        "Resource": [table_arn, table_arn + "/index/*"],
    }],
}))
PY
  echo "attaching DynamoDB runtime policy for table $DDB_TABLE in $DDB_REGION..."
  if ! aws iam put-role-policy --role-name "$ROLE" --policy-name morphdb-dynamodb-runtime \
      --policy-document "file://$POLICY" --profile "$PROFILE" >/dev/null; then
    echo "ERROR: could not attach DynamoDB policy to role '$ROLE'." >&2
    echo "Run ./setup-iam.sh again, then retry ./deploy.sh." >&2
    exit 1
  fi
fi

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
ENVJSON="$(mktemp)"; CLEANUP_FILES+=("$ENVJSON")
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
fi
add_public_permission() {
  SID="$1"; shift
  ERR="$(mktemp)"; CLEANUP_FILES+=("$ERR")
  if aws lambda add-permission --function-name "$FN" --statement-id "$SID" "$@" \
      --region "$REGION" --profile "$PROFILE" >/dev/null 2>"$ERR"; then
    return
  fi
  if grep -q "ResourceConflictException" "$ERR"; then
    return
  fi
  echo "ERROR: could not add public Function URL permission '$SID'." >&2
  cat "$ERR" >&2
  exit 1
}
# Public Function URLs need both permissions: InvokeFunctionUrl authorizes the
# URL action, and InvokeFunction authorizes invokes that arrive via that URL.
add_public_permission public-url \
  --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE
add_public_permission public-url-invoke-function \
  --action lambda:InvokeFunction --principal '*' --invoked-via-function-url
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
