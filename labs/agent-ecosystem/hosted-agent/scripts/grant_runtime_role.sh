#!/usr/bin/env bash
# Grant the Hosted Agent's RUNTIME identity permission to call the model.
#
#   ./scripts/grant_runtime_role.sh <agent-name> <agent-version> [--apply]
#
# WHY THIS IS A SCRIPT AND NOT TERRAFORM
# ---------------------------------------
# The principal does not exist until the agent version does. Foundry mints a
# managed identity WHEN a version is created and reports it as
# `instance_identity.principal_id`; there is no id to grant to beforehand, and
# no Terraform data source that can resolve one for a version that has not been
# created yet. So the ordering is fixed and cannot be flattened into one apply:
#
#     1. create the agent version        (deliberate, human-invoked)
#     2. read its runtime principal_id   (only now does it exist)
#     3. grant the inference role        (this script)
#     4. invoke the agent
#
# Skipping step 3 does not fail the deployment. The version goes `active`, the
# container starts, `/readiness` returns 200 — and every turn then fails,
# because the model call is rejected. That is the trap this script exists to
# close: an agent can be healthy and still be unable to do anything.
#
# THE PRINCIPAL IS NEVER HARD-CODED. It is read back from the live version each
# time. A principal id is per-version and per-environment; committing one would
# be a fact with an expiry date, and re-running against a rebuilt agent would
# silently grant a role to an identity that no longer exists.
#
# IDEMPOTENT. It reads the existing assignments first and does nothing if the
# role is already present, so it is safe to re-run after every deployment.
#
# DRY BY DEFAULT. Creating a role assignment needs human approval, so the script
# PRINTS the command and stops unless `--apply` is passed.
set -euo pipefail

AGENT_NAME="${1:-}"
AGENT_VERSION="${2:-}"
APPLY=0
[ "${3:-}" = "--apply" ] && APPLY=1

if [ -z "$AGENT_NAME" ] || [ -z "$AGENT_VERSION" ]; then
  echo "usage: $0 <agent-name> <agent-version> [--apply]" >&2
  exit 2
fi

: "${FOUNDRY_PROJECT_ENDPOINT:?set it from: terraform -chdir=infrastructure/capabilities/ai-foundry/sandbox output -raw llm_project_endpoint}"
: "${FOUNDRY_RESOURCE_GROUP:=rg-aiplatform-sandbox}"
: "${FOUNDRY_ACCOUNT_NAME:=aif-example-sandbox}"

# The role that grants DATA-PLANE inference on the account. Owner and
# Contributor are management-plane roles and grant none of this.
ROLE="${FOUNDRY_RUNTIME_ROLE:-Cognitive Services OpenAI User}"

echo "==> resolving the runtime identity of ${AGENT_NAME}:${AGENT_VERSION}"
PRINCIPAL_ID="$(
  python - "$AGENT_NAME" "$AGENT_VERSION" <<'PY'
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

name, version = sys.argv[1], sys.argv[2]
client = AIProjectClient(
    endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    credential=DefaultAzureCredential(),
)
payload = client.agents.get_version(agent_name=name, agent_version=version).as_dict()

status = str(payload.get("status", "")).lower()
if status != "active":
    sys.exit(f"version {version} is '{status}', not active; wait before granting")

identity = payload.get("instance_identity") or {}
principal = identity.get("principal_id")
if not principal:
    sys.exit("the version reports no instance_identity.principal_id")
print(principal)
PY
)"

ACCOUNT_ID="$(
  az cognitiveservices account show \
    --resource-group "$FOUNDRY_RESOURCE_GROUP" \
    --name "$FOUNDRY_ACCOUNT_NAME" \
    --query id -o tsv
)"

echo "    principal: $PRINCIPAL_ID"
echo "    scope:     $ACCOUNT_ID"
echo "    role:      $ROLE"

# --- idempotence ------------------------------------------------------------
EXISTING="$(
  az role assignment list \
    --assignee-object-id "$PRINCIPAL_ID" \
    --scope "$ACCOUNT_ID" \
    --include-inherited \
    --query "[?roleDefinitionName=='${ROLE}'] | length(@)" \
    -o tsv 2>/dev/null || echo 0
)"

if [ "${EXISTING:-0}" -gt 0 ]; then
  echo "==> already granted. Nothing to do."
  exit 0
fi

CMD=(az role assignment create
  --assignee-object-id "$PRINCIPAL_ID"
  --assignee-principal-type ServicePrincipal
  --role "$ROLE"
  --scope "$ACCOUNT_ID")

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "==> NOT granted. Creating a role assignment requires human approval."
  echo "    Re-run with --apply, or run this yourself:"
  echo
  printf '    %q ' "${CMD[@]}"; echo
  exit 0
fi

echo "==> granting"
"${CMD[@]}" --output none
echo "==> granted. Data-plane role propagation can take several minutes."
