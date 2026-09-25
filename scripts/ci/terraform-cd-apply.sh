#!/usr/bin/env bash
#
# Apply the EXACT reviewed binary plan for the Terraform CD pipeline.
#
# Post-apply drift verification is intentionally a SEPARATE pipeline step run
# under the plan-only identity (scripts/ci/terraform-cd-drift-check.sh), NOT
# part of this script. Re-planning under the apply identity would report a
# spurious one-resource change because data.azurerm_client_config.current
# .object_id differs between the plan and apply identities; the reviewed plan
# (and applied state) uses the plan identity's value, so verifying under the
# plan identity is the true no-op. Keeping apply (apply WIF) and verification
# (plan WIF) in separate steps preserves identity separation.
#
# Authentication model
# --------------------
# This script is invoked from an `AzureCLI@2` task bound to the APPLY-only
# workload-identity service connection (`sc-azure-terraform-apply`) with
# `addSpnToEnvironment: true`. That task performs a fresh Azure DevOps OIDC
# token request per run and exposes:
#   servicePrincipalId  — the apply managed identity client id
#   idToken             — the short-lived federated OIDC token (SENSITIVE)
#   tenantId            — the Entra tenant id
# No client secret, PAT, storage account key or SAS token is used or accepted.
#
# Exact-plan promotion
# --------------------
# The plan applied here is the binary plan produced BEFORE approval by the plan
# stage (generated with `sc-azure-terraform-plan`) and carried forward as a
# short-lived pipeline artifact. This script applies that saved plan verbatim
# and NEVER re-runs `terraform plan` before apply — the bytes the human
# approved are the bytes that apply. Applying a saved plan needs no variables
# and no `-auto-approve`; Terraform reads the decided values from the file.
#
# This script NEVER runs `terraform destroy`.
#
# Arguments:
#   $1  working directory of the Terraform configuration (e.g. "bootstrap")
#   $2  backend config file, repo-relative (e.g. "backend/dev.hcl")
#   $3  saved binary plan file (absolute path to the downloaded artifact)
#
set -euo pipefail

WORKING_DIR="${1:?working directory required (arg 1)}"
BACKEND_CONFIG="${2:?backend config file required (arg 2)}"
PLAN_FILE="${3:?saved binary plan file required (arg 3)}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# --- Values supplied by the apply workload-identity service connection --------
: "${servicePrincipalId:?service connection did not provide servicePrincipalId (is addSpnToEnvironment set?)}"
: "${idToken:?service connection did not provide an OIDC idToken (is this a workload-identity connection?)}"
: "${tenantId:?service connection did not provide tenantId}"

# --- The reviewed binary plan must exist -------------------------------------
[[ -f "${PLAN_FILE}" ]] || fail "reviewed binary plan not found: ${PLAN_FILE} (the plan-stage artifact must be downloaded before apply)"

BACKEND_ABS="${PWD}/${BACKEND_CONFIG}"
[[ -f "${BACKEND_ABS}" ]] || fail "backend config not found: ${BACKEND_ABS}"

LOCK_TIMEOUT="300s"

# Resolve the subscription bound to the service connection (no secret involved).
subscription_id="$(az account show --query id -o tsv)" \
  || fail "could not resolve the subscription from the service connection"

# --- Transient, least-privilege Terraform authentication environment ---------
# Only the variables Terraform needs; all derived from the fresh OIDC exchange.
export ARM_USE_OIDC="true"
export ARM_USE_AZUREAD="true"
export ARM_CLIENT_ID="${servicePrincipalId}"
export ARM_TENANT_ID="${tenantId}"
export ARM_SUBSCRIPTION_ID="${subscription_id}"
export ARM_OIDC_TOKEN="${idToken}" # SENSITIVE — never echo this value

# Applying a saved binary plan needs NO input variables — the decided values are
# baked into the plan file. (The post-apply drift check, which IS a fresh plan,
# lives in scripts/ci/terraform-cd-drift-check.sh and supplies its own vars.)

echo "==> terraform init (${WORKING_DIR}) against ${BACKEND_CONFIG} [Entra OIDC, no shared key]"
terraform -chdir="${WORKING_DIR}" init \
  -reconfigure -input=false \
  -backend-config="${BACKEND_ABS}" \
  || fail "terraform init failed — verify Entra data-plane (Storage Blob Data Contributor) access to the state container"

echo "==> terraform apply of the EXACT reviewed binary plan: ${PLAN_FILE}"
echo "    (no re-plan; the approved plan is applied verbatim)"
terraform -chdir="${WORKING_DIR}" apply \
  -input=false -lock-timeout="${LOCK_TIMEOUT}" \
  "${PLAN_FILE}" \
  || fail "terraform apply failed — see log; state may hold a lock, recover per docs/runbooks/terraform-state-recovery.md"

echo "Apply complete."
echo "    Post-apply drift verification runs as a separate pipeline step under"
echo "    the plan-only identity (scripts/ci/terraform-cd-drift-check.sh)."
