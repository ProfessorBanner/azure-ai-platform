#!/usr/bin/env bash
#
# Verify, read-only and against LIVE Azure, that one environment's networking
# is in the expected platform mode, and that everything idle mode must retain
# is still there. Exits 1 on the first mismatch.
#
#   idle    NAT gateway absent; NAT public IP present, same address, unattached;
#           both storage private endpoints and their NICs absent; no A records
#           left in either private DNS zone; zones + VNet links present;
#           subnets, NSG associations, storage firewall posture, workspace and
#           Access Connector unchanged.
#   active  NAT gateway present and attached to both Databricks subnets with the
#           retained public IP; both private endpoints Approved; each zone has
#           exactly one A record resolving to the matching endpoint IP; storage
#           firewall posture unchanged.
#
# Then runs `terraform plan -detailed-exitcode` for the root and requires exit 0
# (no changes), i.e. the live state matches the COMMITTED mode.
#
# Usage:
#   scripts/idle/verify-network-mode.sh <env> <idle|active> [--skip-plan]
# Requires: az (logged in), terraform (root initialised), TF_VAR_subscription_id,
#           TF_VAR_storage_account_name (unless --skip-plan). For dev/stg/prod
#           the Databricks provider also needs auth: DATABRICKS_CONFIG_PROFILE or
#           DATABRICKS_AUTH_TYPE=azure-cli.
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

env="${1:?environment required (sandbox|dev|stg|prod)}"
mode="${2:?mode required (idle|active)}"
validate_env "${env}"
[[ "${mode}" == "idle" || "${mode}" == "active" ]] || fail "mode must be idle or active"
skip_plan="false"
[[ "${3:-}" == "--skip-plan" ]] && skip_plan="true"
require_cmd az
require_cmd python3

rg="$(env_rg "${env}")"
sa="$(env_storage_account "${env}")"
vnet="$(env_vnet "${env}")"
nat="$(env_nat "${env}")"
pip="$(env_pip "${env}")"
failures=0

check() { # check <description> <actual> <expected>
  if [[ "$2" == "$3" ]]; then
    echo "  OK    $1: $2"
  else
    echo "  FAIL  $1: got '$2', expected '$3'"
    failures=$((failures + 1))
  fi
}

info "Verifying ${env} networking is in '${mode}' mode (resource group ${rg})"

# --- NAT gateway -------------------------------------------------------------
nat_exists="$(az network nat gateway show -g "${rg}" -n "${nat}" --query name -o tsv 2>/dev/null || true)"
pip_json="$(az network public-ip show -g "${rg}" -n "${pip}" -o json)"
pip_addr="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["ipAddress"])' <<<"${pip_json}")"
pip_attached="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print("attached" if (d.get("natGateway") or {}).get("id") else "unattached")' <<<"${pip_json}")"
pip_alloc="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["publicIPAllocationMethod"], d["sku"]["name"])' <<<"${pip_json}")"
host_nat="$(az network vnet subnet show -g "${rg}" --vnet-name "${vnet}" -n "snet-dbx-host-${env}" --query 'natGateway.id' -o tsv 2>/dev/null || true)"
cont_nat="$(az network vnet subnet show -g "${rg}" --vnet-name "${vnet}" -n "snet-dbx-container-${env}" --query 'natGateway.id' -o tsv 2>/dev/null || true)"

echo "  INFO  retained NAT public IP ${pip}: ${pip_addr} (${pip_alloc})"
check "public IP allocation" "${pip_alloc}" "Static Standard"
if [[ "${mode}" == "idle" ]]; then
  check "NAT gateway ${nat} absent" "${nat_exists:-<absent>}" "<absent>"
  check "public IP unattached" "${pip_attached}" "unattached"
  check "host subnet has no NAT" "${host_nat:-<none>}" "<none>"
  check "container subnet has no NAT" "${cont_nat:-<none>}" "<none>"
else
  check "NAT gateway ${nat} present" "${nat_exists}" "${nat}"
  check "public IP attached" "${pip_attached}" "attached"
  check "host subnet NAT" "${host_nat##*/}" "${nat}"
  check "container subnet NAT" "${cont_nat##*/}" "${nat}"
fi

# --- Private endpoints + DNS -------------------------------------------------
for sub in dfs blob; do
  pe="pe-${sa}-${sub}"
  zone="privatelink.${sub}.core.windows.net"
  pe_state="$(az network private-endpoint show -g "${rg}" -n "${pe}" --query 'privateLinkServiceConnections[0].privateLinkServiceConnectionState.status' -o tsv 2>/dev/null || true)"
  pe_nic_id="$(az network private-endpoint show -g "${rg}" -n "${pe}" --query 'networkInterfaces[0].id' -o tsv 2>/dev/null || true)"
  pe_ip=""
  [[ -n "${pe_nic_id}" ]] && pe_ip="$(az network nic show --ids "${pe_nic_id}" --query 'ipConfigurations[0].privateIPAddress' -o tsv 2>/dev/null || true)"
  nic_count="$(az network nic list -g "${rg}" --query "length([?starts_with(name, '${pe}.nic.')])" -o tsv)"
  a_records="$(az network private-dns record-set a list -g "${rg}" -z "${zone}" -o tsv --query '[].[name,aRecords[0].ipv4Address]' 2>/dev/null | tr '\t' '=' | tr '\n' ' ' || true)"
  a_count="$(az network private-dns record-set a list -g "${rg}" -z "${zone}" --query 'length(@)' -o tsv 2>/dev/null || echo "zone-missing")"
  link_state="$(az network private-dns link vnet show -g "${rg}" -z "${zone}" -n "link-${vnet}-${sub}" --query 'virtualNetworkLinkState' -o tsv 2>/dev/null || true)"
  check "private DNS zone ${zone} retained" "$([[ "${a_count}" == "zone-missing" ]] && echo missing || echo present)" "present"
  check "VNet link link-${vnet}-${sub} retained" "${link_state}" "Completed"
  if [[ "${mode}" == "idle" ]]; then
    check "private endpoint ${pe} absent" "${pe_state:-<absent>}" "<absent>"
    check "private endpoint NIC(s) for ${pe} absent" "${nic_count}" "0"
    check "no stale A records in ${zone}" "${a_count}" "0"
  else
    check "private endpoint ${pe} approved" "${pe_state}" "Approved"
    check "private endpoint NIC for ${pe} present" "${nic_count}" "1"
    check "A record count in ${zone}" "${a_count}" "1"
    check "A record in ${zone}" "${a_records% }" "${sa}=${pe_ip}"
  fi
done

# --- Things idle mode must never change --------------------------------------
sa_json="$(az storage account show -n "${sa}" -o json)"
check "storage publicNetworkAccess" "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["publicNetworkAccess"])' <<<"${sa_json}")" "Disabled"
check "storage firewall defaultAction" "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["networkRuleSet"]["defaultAction"])' <<<"${sa_json}")" "Deny"
check "storage shared key disabled" "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["allowSharedKeyAccess"])' <<<"${sa_json}")" "False"
check "Databricks workspace present" "$(az databricks workspace show -g "${rg}" -n "dbw-aiplatform-${env}" --query provisioningState -o tsv)" "Succeeded"
check "Access Connector present" "$(az resource show -g "${rg}" -n "ac-aiplatform-databricks-${env}" --resource-type Microsoft.Databricks/accessConnectors --query name -o tsv)" "ac-aiplatform-databricks-${env}"
check "Key Vault present" "$(az keyvault list -g "${rg}" --query 'length(@)' -o tsv)" "1"
check "subnets present" "$(az network vnet subnet list -g "${rg}" --vnet-name "${vnet}" --query 'length(@)' -o tsv)" "3"
check "NSG associated to host subnet" "$(az network vnet subnet show -g "${rg}" --vnet-name "${vnet}" -n "snet-dbx-host-${env}" --query 'networkSecurityGroup.id' -o tsv | xargs basename)" "nsg-dbx-${env}"
check "NSG associated to container subnet" "$(az network vnet subnet show -g "${rg}" --vnet-name "${vnet}" -n "snet-dbx-container-${env}" --query 'networkSecurityGroup.id' -o tsv | xargs basename)" "nsg-dbx-${env}"

# --- Terraform: live state must match the COMMITTED mode ----------------------
if [[ "${skip_plan}" == "false" ]]; then
  require_cmd terraform
  root="$(env_root "${env}")"
  info "terraform plan -detailed-exitcode (${root}); expecting 0 = no changes"
  set +e
  terraform -chdir="${root}" plan -input=false -lock-timeout=60s -detailed-exitcode >/dev/null 2>"$(env_tmp "${env}")/verify-plan.err"
  rc=$?
  set -e
  case "${rc}" in
    0) echo "  OK    terraform plan: no changes (live == committed configuration)" ;;
    2) echo "  FAIL  terraform plan: changes pending — the committed mode does not match Azure (before the first apply of the idle-mode code this is the expected 'moved' state; after any apply it means drift)"; failures=$((failures + 1)) ;;
    *) echo "  FAIL  terraform plan errored (see tmp/idle/${env}/verify-plan.err)"; failures=$((failures + 1)) ;;
  esac
  committed_mode="$(terraform -chdir="${root}" output -raw platform_mode 2>/dev/null || echo unknown)"
  check "committed platform_mode output" "${committed_mode}" "${mode}"
fi

if [[ "${failures}" -eq 0 ]]; then
  info "PASS: ${env} is in '${mode}' mode and all retained resources are intact."
else
  fail "${failures} check(s) failed for ${env} (${mode})."
fi
