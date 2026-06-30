#!/usr/bin/env bash
# One-time IAM bootstrap for the MorphDB Lambda deploy. Run ONCE with an admin
# identity (it creates a role + a scoped deploy user, then a CLI profile the
# other scripts use). Idempotent: safe to re-run.
#
#   ./setup-iam.sh
#
# Creates:
#   * role  morphdb-lambda-exec  (Lambda execution role; CloudWatch logs, plus
#                                  a DynamoDB table policy when deploy.sh needs it)
#   * user  morphdb-deploy       (Lambda + ECR + Logs + PassRole/role-policy
#                                  management on the execution role)
#   * aws profile 'morphdb'      (access keys stored in ~/.aws/credentials, 0600)
set -euo pipefail

REGION="${MORPHDB_AWS_REGION:-us-west-1}"
ADMIN_PROFILE="${MORPHDB_ADMIN_PROFILE:-default}"   # identity used to create IAM
DEPLOY_PROFILE="${MORPHDB_DEPLOY_PROFILE:-morphdb}"
ROLE=morphdb-lambda-exec
USER=morphdb-deploy

ACCT="$(aws sts get-caller-identity --profile "$ADMIN_PROFILE" --query Account --output text)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

cat >"$TMP/trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
aws iam create-role --role-name "$ROLE" --assume-role-policy-document "file://$TMP/trust.json" \
  --profile "$ADMIN_PROFILE" >/dev/null 2>&1 && echo "role $ROLE: created" || echo "role $ROLE: exists"
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
  --profile "$ADMIN_PROFILE"

aws iam create-user --user-name "$USER" --profile "$ADMIN_PROFILE" >/dev/null 2>&1 \
  && echo "user $USER: created" || echo "user $USER: exists"
for P in arn:aws:iam::aws:policy/AWSLambda_FullAccess \
         arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess \
         arn:aws:iam::aws:policy/CloudWatchLogsFullAccess; do
  aws iam attach-user-policy --user-name "$USER" --policy-arn "$P" --profile "$ADMIN_PROFILE"
done
cat >"$TMP/passrole.json" <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::${ACCT}:role/${ROLE}"},
  {"Effect":"Allow","Action":["iam:GetRole","iam:GetRolePolicy","iam:PutRolePolicy","iam:DeleteRolePolicy"],"Resource":"arn:aws:iam::${ACCT}:role/${ROLE}"}
]}
JSON
aws iam put-user-policy --user-name "$USER" --policy-name morphdb-passrole \
  --policy-document "file://$TMP/passrole.json" --profile "$ADMIN_PROFILE"
echo "user $USER: policies attached"

if aws sts get-caller-identity --profile "$DEPLOY_PROFILE" >/dev/null 2>&1; then
  echo "profile $DEPLOY_PROFILE: already configured"
else
  read -r AKID SAK < <(aws iam create-access-key --user-name "$USER" \
      --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text --profile "$ADMIN_PROFILE")
  aws configure set aws_access_key_id "$AKID" --profile "$DEPLOY_PROFILE"
  aws configure set aws_secret_access_key "$SAK" --profile "$DEPLOY_PROFILE"
  aws configure set region "$REGION" --profile "$DEPLOY_PROFILE"
  unset AKID SAK
  echo "profile $DEPLOY_PROFILE: configured (keys in ~/.aws/credentials, not printed)"
fi
echo "done. Now run ./deploy.sh"
