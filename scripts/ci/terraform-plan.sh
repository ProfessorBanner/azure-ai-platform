#!/usr/bin/env bash
#
# Run a read-only Terraform plan for CI.
#
# Authentication model
# --------------------
# This script is invoked from an `AzureCLI@2` task bound to the plan-only
# workload-identity service connection (`sc-aiplatform-tf-plan`) with
# `addSpnToEnvironment: true`. That task performs a fresh Azure DevOps OIDC
# token request per run and exposes:
#   servicePrincipalId  — the user-assigned managed identity client id
#   idToken             — the short-lived federated OIDC token (SENSITIVE)
#   tenantId            — the Entra tenant id
# No client secret, PAT, storage key or SAS token is used or accepted.
#
# The OIDC token is treated as a secret: it is never echoed, and the produced
# artifacts are scanned to guarantee it did not leak.
#
# This script NEVER runs `terraform apply` or `terraform destroy`.
#
# Arguments:
#   $1  working directory of the Terraform configuration (e.g. "bootstrap")
#   $2  backend config file, repo-relative (e.g. "backend/dev.hcl")
#
set -euo pipefail

WORKING_DIR="${1:?working directory required (arg 1)}"
BACKEND_CONFIG="${2:?backend config file required (arg 2)}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# --- Values supplied by the workload-identity service connection -------------
: "${servicePrincipalId:?service connection did not provide servicePrincipalId (is addSpnToEnvironment set?)}"
: "${idToken:?service connection did not provide an OIDC idToken (is this a workload-identity connection?)}"
: "${tenantId:?service connection did not provide tenantId}"

# --- Artifact staging (published) vs binary plan (workspace-only) ------------
STAGING="${BUILD_ARTIFACTSTAGINGDIRECTORY:?BUILD_ARTIFACTSTAGINGDIRECTORY must be set}"
LOG_DIR="${STAGING}/logs"
PLAN_DIR="${STAGING}/plan"
mkdir -p "${LOG_DIR}" "${PLAN_DIR}"

BACKEND_ABS="${PWD}/${BACKEND_CONFIG}"
[[ -f "${BACKEND_ABS}" ]] || fail "backend config not found: ${BACKEND_ABS}"

# The binary plan stays inside the workspace and is NEVER published.
BINARY_PLAN="terraform.tfplan" # relative to -chdir; matches *.tfplan gitignore

# Resolve the subscription bound to the service connection (no secret involved).
subscription_id="$(az account show --query id -o tsv)" \
  || fail "could not resolve the subscription from the service connection"

# --- Terraform input variables (TF_VAR_*) ------------------------------------
# bootstrap/variables.tf declares subscription_id, resource_group_name and
# storage_account_name with no default. They normally come from the git-ignored
# bootstrap.auto.tfvars, which does not exist on the hosted agent. Supply them
# via TF_VAR_* instead: subscription_id from the resolved subscription above,
# resource_group_name / storage_account_name injected by the pipeline `env:`
# block. location and container_name keep their defaults. No secret is involved.
: "${TF_VAR_resource_group_name:?TF_VAR_resource_group_name required (set it in the pipeline env block)}"
: "${TF_VAR_storage_account_name:?TF_VAR_storage_account_name required (set it in the pipeline env block)}"
export TF_VAR_subscription_id="${subscription_id}"

# --- Platform mode is COMMITTED, never injected ------------------------------
# infrastructure/environments/<env>/platform_mode.tf is the only source of
# var.idle_mode. Refuse to run if anything tries to override it from the
# environment, so a pipeline parameter can never flip an environment's mode
# (docs/runbooks/platform-idle-mode.md, ADR 0013).
if [[ -n "${TF_VAR_idle_mode:-}" ]]; then
  fail "TF_VAR_idle_mode must not be set: the platform mode is committed in platform_mode.tf"
fi

# --- Transient, least-privilege Terraform authentication environment ---------
# Only the variables Terraform needs; all derived from the fresh OIDC exchange.
export ARM_USE_OIDC="true"
export ARM_USE_AZUREAD="true"
export ARM_CLIENT_ID="${servicePrincipalId}"
export ARM_TENANT_ID="${tenantId}"
export ARM_SUBSCRIPTION_ID="${subscription_id}"
export ARM_OIDC_TOKEN="${idToken}" # SENSITIVE — never echo this value

echo "==> terraform init (${WORKING_DIR}) against ${BACKEND_CONFIG} [Entra OIDC, no shared key]"
terraform -chdir="${WORKING_DIR}" init \
  -reconfigure -input=false \
  -backend-config="${BACKEND_ABS}" \
  2>&1 | tee "${LOG_DIR}/terraform-init.log" \
  || fail "terraform init failed — verify Entra data-plane (Storage Blob Data Contributor) access to the state container"

echo "==> terraform validate (${WORKING_DIR})"
terraform -chdir="${WORKING_DIR}" validate \
  2>&1 | tee "${LOG_DIR}/terraform-validate.log" \
  || fail "terraform validate failed"

echo "==> terraform plan (${WORKING_DIR}) — read-only, state lock with timeout"
terraform -chdir="${WORKING_DIR}" plan \
  -input=false -lock-timeout=120s -out="${BINARY_PLAN}" \
  2>&1 | tee "${LOG_DIR}/terraform-plan.log" \
  || fail "terraform plan failed"

echo "==> Rendering human-readable plan"
terraform -chdir="${WORKING_DIR}" show -no-color "${BINARY_PLAN}" > "${PLAN_DIR}/plan.raw.txt" \
  || fail "terraform show failed"

# --- Redact any sensitive-looking assignments before publishing --------------
# Terraform already masks values marked `sensitive`; this is defence in depth
# against tokens/secrets/keys that might otherwise surface in plan output.
sed -E \
  -e 's/((secret|password|token|access_key|primary_access_key|sas_token|client_secret)[^=]*=[[:space:]]*)"[^"]*"/\1"***REDACTED***"/Ig' \
  "${PLAN_DIR}/plan.raw.txt" > "${PLAN_DIR}/plan.txt"
rm -f "${PLAN_DIR}/plan.raw.txt"

# --- Guarantee the OIDC token never leaked into any published artifact --------
if grep -rqF -- "${idToken}" "${STAGING}"; then
  # Do not print the token or the offending line.
  rm -rf "${STAGING:?}/"*
  fail "OIDC token detected in staged artifacts — publishing aborted"
fi

echo "Plan complete."
echo "  Human-readable plan : ${PLAN_DIR}/plan.txt"
echo "  Validation logs     : ${LOG_DIR}/"
echo "  Binary plan (workspace-only, not published): ${WORKING_DIR}/${BINARY_PLAN}"
