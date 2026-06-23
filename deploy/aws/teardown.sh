#!/usr/bin/env bash
# Remove the MorphDB Lambda deployment. By default deletes the function + its
# public URL. Pass --iam to also remove the deploy user, role, and profile.
#
#   ./teardown.sh           # delete the function + Function URL
#   ./teardown.sh --iam     # ...and the IAM user/role created by setup-iam.sh
#
# Your data lives in the external database (Neon/RDS), NOT in Lambda — tearing
# this down removes the public endpoint only; the database is untouched.
set -euo pipefail

PROFILE="${MORPHDB_DEPLOY_PROFILE:-morphdb}"
REGION="${MORPHDB_AWS_REGION:-us-west-1}"
FN="${MORPHDB_FN_NAME:-morphdb-api}"
ROLE=morphdb-lambda-exec
USER=morphdb-deploy

aws lambda delete-function-url-config --function-name "$FN" --region "$REGION" --profile "$PROFILE" 2>/dev/null \
  && echo "deleted function URL" || echo "no function URL"
aws lambda delete-function --function-name "$FN" --region "$REGION" --profile "$PROFILE" 2>/dev/null \
  && echo "deleted function $FN" || echo "no function $FN"

if [ "${1:-}" = "--iam" ]; then
  ADMIN_PROFILE="${MORPHDB_ADMIN_PROFILE:-default}"
  aws iam delete-user-policy --user-name "$USER" --policy-name morphdb-passrole --profile "$ADMIN_PROFILE" 2>/dev/null || true
  for P in arn:aws:iam::aws:policy/AWSLambda_FullAccess \
           arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess \
           arn:aws:iam::aws:policy/CloudWatchLogsFullAccess; do
    aws iam detach-user-policy --user-name "$USER" --policy-arn "$P" --profile "$ADMIN_PROFILE" 2>/dev/null || true
  done
  for K in $(aws iam list-access-keys --user-name "$USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text --profile "$ADMIN_PROFILE" 2>/dev/null); do
    aws iam delete-access-key --user-name "$USER" --access-key-id "$K" --profile "$ADMIN_PROFILE" 2>/dev/null || true
  done
  aws iam delete-user --user-name "$USER" --profile "$ADMIN_PROFILE" 2>/dev/null && echo "deleted user $USER" || true
  aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole --profile "$ADMIN_PROFILE" 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE" --profile "$ADMIN_PROFILE" 2>/dev/null && echo "deleted role $ROLE" || true
fi
echo "done."
