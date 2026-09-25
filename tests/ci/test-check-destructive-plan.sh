#!/usr/bin/env bash
#
# Table-driven local tests for scripts/ci/check-destructive-plan.sh.
#
# The guard's whole contract is a pure function of the plan JSON, so it is
# proven here at L0 with synthetic `terraform show -json` documents: no
# terraform binary, no Azure, no state. Each case asserts BOTH the exit status
# and a message fragment, so a guard that failed for the wrong reason does not
# pass.
#
# The idle-mode contract under test (docs/runbooks/platform-idle-mode.md):
#   - idle_mode absent/false: ANY delete or replace fails (legacy behaviour).
#   - idle_mode true (bool from a committed default, or the string "true" from
#     a -var override): pure deletes of the NAT gateway, its associations and
#     the storage private endpoints — inside module.network /
#     module.storage_private_access — are allowed; everything else still fails.
#
# Usage:  ./tests/ci/test-check-destructive-plan.sh
# Exit:   0 all tests passed, 1 one or more failed.
#
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
guard="${repo_root}/scripts/ci/check-destructive-plan.sh"
template="${repo_root}/azure-pipelines/templates/terraform-cd-stages.yml"
[[ -x "${guard}" ]] || { echo "FATAL: ${guard} missing or not executable"; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# plan_json <idle_value|none> <resource_changes JSON array>
plan_json() {
  local idle="$1" changes="$2" vars
  case "${idle}" in
    none) vars='{}' ;;
    bool-true) vars='{"idle_mode":{"value":true}}' ;;
    str-true) vars='{"idle_mode":{"value":"true"}}' ;;
    bool-false) vars='{"idle_mode":{"value":false}}' ;;
    str-false) vars='{"idle_mode":{"value":"false"}}' ;;
  esac
  printf '{"format_version":"1.2","variables":%s,"resource_changes":%s}\n' "${vars}" "${changes}"
}

rc() { printf '{"address":"%s","type":"%s","change":{"actions":[%s]}}' "$1" "$2" "$3"; }

NAT_DELETES="[$(rc module.network.azurerm_nat_gateway.databricks[0] azurerm_nat_gateway '"delete"'),\
$(rc module.network.azurerm_nat_gateway_public_ip_association.databricks[0] azurerm_nat_gateway_public_ip_association '"delete"'),\
$(rc module.network.azurerm_subnet_nat_gateway_association.host[0] azurerm_subnet_nat_gateway_association '"delete"'),\
$(rc module.network.azurerm_subnet_nat_gateway_association.container[0] azurerm_subnet_nat_gateway_association '"delete"'),\
$(rc module.storage_private_access.azurerm_private_endpoint.dfs[0] azurerm_private_endpoint '"delete"'),\
$(rc module.storage_private_access.azurerm_private_endpoint.blob[0] azurerm_private_endpoint '"delete"')]"
NOOP="[$(rc module.storage.azurerm_storage_account.this azurerm_storage_account '"no-op"')]"
CREATES="[$(rc module.network.azurerm_nat_gateway.databricks[0] azurerm_nat_gateway '"create"')]"
PIP_DELETE="[$(rc module.network.azurerm_public_ip.databricks_nat azurerm_public_ip '"delete"')]"
ZONE_DELETE="[$(rc module.storage_private_access.azurerm_private_dns_zone.dfs azurerm_private_dns_zone '"delete"')]"
LINK_DELETE="[$(rc module.storage_private_access.azurerm_private_dns_zone_virtual_network_link.blob azurerm_private_dns_zone_virtual_network_link '"delete"')]"
STORAGE_DELETE="[$(rc module.storage.azurerm_storage_account.this azurerm_storage_account '"delete"')]"
PE_REPLACE="[$(rc module.storage_private_access.azurerm_private_endpoint.dfs[0] azurerm_private_endpoint '"delete","create"')]"
WORKSPACE_REPLACE="[$(rc module.databricks_workspace.azurerm_databricks_workspace.this azurerm_databricks_workspace '"create","delete"')]"
WRONG_MODULE_PE="[$(rc module.other.azurerm_private_endpoint.x azurerm_private_endpoint '"delete"')]"
SAME_TYPE_OTHER_PE="[$(rc module.storage_private_access.azurerm_private_endpoint.queue[0] azurerm_private_endpoint '"delete"')]"
SAME_TYPE_OTHER_NAT="[$(rc module.network.azurerm_nat_gateway.other[0] azurerm_nat_gateway '"delete"')]"
OTHER_INDEX_NAT="[$(rc module.network.azurerm_nat_gateway.databricks[1] azurerm_nat_gateway '"delete"')]"
UNINDEXED_NAT="[$(rc module.network.azurerm_nat_gateway.databricks azurerm_nat_gateway '"delete"')]"
NAT_UPDATE_ONLY="[$(rc module.network.azurerm_nat_gateway.databricks[0] azurerm_nat_gateway '"update"')]"
ROOT_NAT="[$(rc azurerm_nat_gateway.rogue azurerm_nat_gateway '"delete"')]"
MIXED="[$(rc module.network.azurerm_nat_gateway.databricks[0] azurerm_nat_gateway '"delete"'),$(rc module.storage.azurerm_storage_account.this azurerm_storage_account '"delete"')]"

# name | idle | changes | expected_rc | expected fragment
CASES=(
  "no-op plan, no idle var|none|${NOOP}|0|No destructive (delete/replace) changes found"
  "creates only (restore), idle false|bool-false|${CREATES}|0|No destructive (delete/replace) changes found"
  "idle deletes with idle var ABSENT fail|none|${NAT_DELETES}|1|module.network.azurerm_nat_gateway.databricks[0]: delete"
  "idle deletes with idle=false fail|bool-false|${NAT_DELETES}|1|destructive change(s) detected"
  "idle deletes with idle=\"false\" (string) fail|str-false|${NAT_DELETES}|1|destructive change(s) detected"
  "idle deletes with idle=true (bool) pass|bool-true|${NAT_DELETES}|0|Only expected idle-mode removals found"
  "idle deletes with idle=\"true\" (string) pass|str-true|${NAT_DELETES}|0|Only expected idle-mode removals found"
  "idle=true never allows deleting the public IP|bool-true|${PIP_DELETE}|1|azurerm_public_ip.databricks_nat: delete"
  "idle=true never allows deleting a DNS zone|bool-true|${ZONE_DELETE}|1|azurerm_private_dns_zone.dfs: delete"
  "idle=true never allows deleting a VNet link|bool-true|${LINK_DELETE}|1|virtual_network_link.blob: delete"
  "idle=true never allows deleting storage|bool-true|${STORAGE_DELETE}|1|azurerm_storage_account.this: delete"
  "idle=true never allows a replacement (PE)|bool-true|${PE_REPLACE}|1|azurerm_private_endpoint.dfs[0]: replace"
  "idle=true never allows a replacement (workspace)|bool-true|${WORKSPACE_REPLACE}|1|azurerm_databricks_workspace.this: replace"
  "idle=true: allow-listed type in the wrong module fails|bool-true|${WRONG_MODULE_PE}|1|module.other.azurerm_private_endpoint.x: delete"
  "idle=true: allow-listed type at root fails|bool-true|${ROOT_NAT}|1|azurerm_nat_gateway.rogue: delete"
  "idle=true: same type, other private endpoint in the module fails|bool-true|${SAME_TYPE_OTHER_PE}|1|azurerm_private_endpoint.queue[0]: delete"
  "idle=true: same type, other NAT gateway in the module fails|bool-true|${SAME_TYPE_OTHER_NAT}|1|azurerm_nat_gateway.other[0]: delete"
  "idle=true: allowed address at another index fails|bool-true|${OTHER_INDEX_NAT}|1|azurerm_nat_gateway.databricks[1]: delete"
  "idle=true: allowed address without index fails|bool-true|${UNINDEXED_NAT}|1|azurerm_nat_gateway.databricks: delete"
  "idle=true: in-place update of an allowed address is not a delete (passes)|bool-true|${NAT_UPDATE_ONLY}|0|delete=0"
  "idle=true: one allowed + one forbidden delete fails|bool-true|${MIXED}|1|azurerm_storage_account.this: delete"
)

pass=0
failcount=0
i=0
for row in "${CASES[@]}"; do
  i=$((i + 1))
  IFS='|' read -r name idle changes want_rc want_msg <<<"${row}"
  file="${work}/case-${i}.json"
  plan_json "${idle}" "${changes}" >"${file}"
  out="$("${guard}" "${repo_root}" "${file}" 2>&1)"
  got_rc=$?
  if [[ "${got_rc}" -eq "${want_rc}" && "${out}" == *"${want_msg}"* ]]; then
    echo "PASS  ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL  ${name}"
    echo "      expected rc=${want_rc} containing: ${want_msg}"
    echo "      got rc=${got_rc}; output:"
    sed 's/^/      | /' <<<"${out}"
    failcount=$((failcount + 1))
  fi
done

# Wiring: the CD template must still invoke the guard on the fresh plan.
if grep -q 'scripts/ci/check-destructive-plan.sh "${{ parameters.terraformWorkingDirectory }}" "${PLAN_FILE}"' "${template}"; then
  echo "PASS  terraform-cd-stages.yml invokes the guard on the plan"
  pass=$((pass + 1))
else
  echo "FAIL  terraform-cd-stages.yml no longer invokes the guard as expected"
  failcount=$((failcount + 1))
fi

echo
echo "${pass} passed, ${failcount} failed"
[[ "${failcount}" -eq 0 ]]
