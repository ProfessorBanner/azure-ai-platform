#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${1:-}"

case "$ENVIRONMENT" in
  dev|stg|prod)
    ;;
  *)
    echo "Usage: $0 {dev|stg|prod}" >&2
    exit 1
    ;;
esac

for cmd in git databricks jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    exit 1
  fi
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
PRODUCT_DIR="$REPO_ROOT/products/hello-databricks"
DATABRICKS_PROFILE="aiplatform-${ENVIRONMENT}"

BUNDLE_JSON="$(
  cd "$PRODUCT_DIR"
  databricks bundle validate \
    -t "$ENVIRONMENT" \
    -p "$DATABRICKS_PROFILE" \
    -o json
)"

WORKSPACE_HOST="$(
  jq -r '.workspace.host // empty' <<<"$BUNDLE_JSON"
)"

CATALOG="$(
  jq -r '.variables.catalog.value // empty' <<<"$BUNDLE_JSON"
)"

SCHEMA="$(
  jq -r '.variables.schema.value // empty' <<<"$BUNDLE_JSON"
)"

JOB_COMPUTE_POLICY_ID="$(
  jq -r '.variables.job_compute_policy_id.value // empty' <<<"$BUNDLE_JSON"
)"

RUN_AS_CLIENT_ID="$(
  jq -r \
    '.resources.jobs.hello_databricks.run_as.service_principal_name // empty' \
    <<<"$BUNDLE_JSON"
)"

RUN_AS_SP_NAME=""

if command -v az >/dev/null 2>&1 \
  && az account show >/dev/null 2>&1 \
  && [[ -n "$RUN_AS_CLIENT_ID" ]]; then
  RUN_AS_SP_NAME="$(
    az ad sp show \
      --id "$RUN_AS_CLIENT_ID" \
      --query displayName \
      -o tsv \
      2>/dev/null || true
  )"
fi

printf '%-26s %s\n' "ENVIRONMENT" "$ENVIRONMENT"
printf '%-26s %s\n' "DATABRICKS_PROFILE" "$DATABRICKS_PROFILE"
printf '%-26s %s\n' "WORKSPACE_HOST" "$WORKSPACE_HOST"
printf '%-26s %s\n' "CATALOG" "$CATALOG"
printf '%-26s %s\n' "SCHEMA" "$SCHEMA"
printf '%-26s %s\n' "JOB_COMPUTE_POLICY_ID" "$JOB_COMPUTE_POLICY_ID"
printf '%-26s %s\n' "RUN_AS_CLIENT_ID" "$RUN_AS_CLIENT_ID"

if [[ -n "$RUN_AS_SP_NAME" ]]; then
  printf '%-26s %s\n' "RUN_AS_SP_NAME" "$RUN_AS_SP_NAME"
fi
