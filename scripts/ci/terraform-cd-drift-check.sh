#!/usr/bin/env bash
#
# Post-apply drift verification for the Terraform CD pipeline.
#
# Runs AFTER scripts/ci/terraform-cd-apply.sh has applied the reviewed binary
# plan. It performs a read-only `terraform plan -detailed-exitcode` and fails if
# any change remains — i.e. it asserts the applied state now matches config.
#
# Why this is a SEPARATE step under the PLAN identity
# ---------------------------------------------------
# This runs under the PLAN-ONLY workload-identity service connection
# (sc-azure-terraform-plan) — the SAME identity that produced the reviewed plan
# — NOT the apply identity. The separation is deliberate: the configuration
# reads data.azurerm_client_config.current, whose object_id differs per
# identity. The reviewed plan (and therefore the applied state) was computed
# with the PLAN identity's object_id, so re-planning under the plan identity is
# a true no-op (exit 0). Re-planning under the APPLY identity would instead
# report a spurious single-resource change (current_principal_object_id) that is
# an identity difference, NOT infrastructure drift. Keeping apply (apply WIF)
# and verification (plan WIF) in separate steps preserves identity separation.
#
# Authentication model
# --------------------
# Invoked from an `AzureCLI@2` task bound to sc-azure-terraform-plan with
# `addSpnToEnvironment: true`, exposing:
#   servicePrincipalId  — the plan managed identity client id
#   idToken             — the short-lived federated OIDC token (SENSITIVE)
#   tenantId            — the Entra tenant id
# No client secret, PAT, storage account key or SAS token is used or accepted.
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

# --- Values supplied by the plan-only workload-identity service connection ----
: "${servicePrincipalId:?service connection did not provide servicePrincipalId (is addSpnToEnvironment set?)}"
: "${idToken:?service connection did not provide an OIDC idToken (is this a workload-identity connection?)}"
: "${tenantId:?service connection did not provide tenantId}"

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

# The drift check is a fresh plan, so it needs the root input variables (same as
# the plan stage). resource_group_name / storage_account_name come from the
# pipeline env block; subscription_id from the resolved subscription above.
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

echo "==> terraform init (${WORKING_DIR}) against ${BACKEND_CONFIG} [Entra OIDC, no shared key]"
terraform -chdir="${WORKING_DIR}" init \
  -reconfigure -input=false \
  -backend-config="${BACKEND_ABS}" \
  || fail "terraform init failed — verify Entra data-plane (Storage Blob Data Contributor) access to the state container"

echo "==> Post-apply drift check: terraform plan -detailed-exitcode (plan-only identity)"
# -detailed-exitcode: 0 = no changes (expected), 2 = changes remain (drift),
# 1 = error. Do not let `set -e` abort before we can interpret the code.
set +e
terraform -chdir="${WORKING_DIR}" plan \
  -input=false -lock-timeout="${LOCK_TIMEOUT}" -detailed-exitcode
drift_rc=$?
set -e

case "${drift_rc}" in
  0)
    echo "==> Post-apply drift check passed: no changes (exit code 0)."
    ;;
  2)
    fail "Post-apply drift detected (exit code 2): resources still differ from state after apply. Investigate before re-running."
    ;;
  *)
    fail "Post-apply plan errored (exit code ${drift_rc})."
    ;;
esac

echo "Drift check complete: state matches configuration."
