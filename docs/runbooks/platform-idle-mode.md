# Runbook: platform idle mode (suspend and restore an environment)

Reversible, per-environment "idle" for `sandbox`, `dev`, `stg` and `prod`:
stop the continuously billed networking (NAT gateway, storage private
endpoints) and pause workloads, while keeping data, models, configuration,
identities and the ability to restore. Decision: ADR 0013.

Every command is written to be run from the repository root of the
`feat/platform-idle-mode` checkout. `<env>` is one of `sandbox dev stg prod`.
Section 5 is the complete sandbox procedure; sections 6 and 7 cover the other
environments and restoration.

---

## 1. Inventory (measured 2026-09-23/24)

Subscription `11111111-1111-1111-1111-111111111111` ("Azure subscription 1"),
region UK South. One resource group per environment, `rg-aiplatform-<env>`,
plus the Databricks-managed group `databricks-rg-rg-aiplatform-<env>`.

### 1.1 Continuously billed networking (all four environments identical)

| Resource | Name | Terraform address | Idle action |
|---|---|---|---|
| NAT gateway (Standard) | `nat-dbx-<env>` | `module.network.azurerm_nat_gateway.databricks[0]` | **delete** |
| NAT ↔ public IP association | — | `module.network.azurerm_nat_gateway_public_ip_association.databricks[0]` | **delete** |
| NAT ↔ host subnet association | `snet-dbx-host-<env>` | `module.network.azurerm_subnet_nat_gateway_association.host[0]` | **delete** |
| NAT ↔ container subnet association | `snet-dbx-container-<env>` | `module.network.azurerm_subnet_nat_gateway_association.container[0]` | **delete** |
| Private endpoint, ADLS `dfs` | `pe-stexample<env>data-dfs` (+ NIC, zone group `default`, A record) | `module.storage_private_access.azurerm_private_endpoint.dfs[0]` | **delete** |
| Private endpoint, blob | `pe-stexample<env>data-blob` (+ NIC, zone group `default`, A record) | `module.storage_private_access.azurerm_private_endpoint.blob[0]` | **delete** |
| Public IP (Standard, static) | `pip-dbx-nat-<env>` | `module.network.azurerm_public_ip.databricks_nat` | retain (unattached) |
| Private DNS zones | `privatelink.dfs.core.windows.net`, `privatelink.blob.core.windows.net` | `module.storage_private_access.azurerm_private_dns_zone.{dfs,blob}` | retain |
| Private DNS VNet links | `link-vnet-aiplatform-<env>-{dfs,blob}` | `module.storage_private_access.azurerm_private_dns_zone_virtual_network_link.{dfs,blob}` | retain |
| VNet, 3 subnets, NSG + 2 associations | `vnet-aiplatform-<env>`, `nsg-dbx-<env>` | `module.network.*` | retain |

Retained NAT public IP addresses (external allowlists may reference them; the
sandbox Foundry account `aif-example-sandbox` allow-lists `203.0.113.250`):

| env | `pip-dbx-nat-<env>` |
|---|---|
| sandbox | 203.0.113.250 |
| dev | 203.0.113.28 |
| stg | 203.0.113.41 |
| prod | 203.0.113.214 |

Not present anywhere: Azure Firewall, VPN/ExpressRoute gateways, Bastion,
load balancers, application gateways, Databricks-managed NAT gateways (the
managed resource groups contain only the DBFS storage account, the
`dbmanagedidentity` and a `unity-catalog-access-connector`). The "Virtual
Network" meter is therefore exactly: Private Link (8 endpoints) + IP
Addresses (4 public IPs).

### 1.2 Data, security and management resources (all retained, untouched)

Per environment: `stexample<env>data` (ADLS Gen2, `publicNetworkAccess =
Disabled`, `defaultAction = Deny`, shared key disabled), `kv-aiplatform-<env>`
(`kv-aiplatform-prod000000` in prod), `log-aiplatform-<env>`,
`dbw-aiplatform-<env>` (Premium, VNet-injected, SCC, public control-plane
access **enabled**), `ac-aiplatform-databricks-<env>`, Unity Catalog storage
credential / external location / catalog (dev, stg, prod).

Terraform backend: `stexampletf000000` in `rg-aiplatform-bootstrap-dev`,
container `tfstate`, keys `platform/<env>.tfstate`; public network access
enabled, no private endpoint, Entra ID auth. Backend access does not depend
on any resource idle mode removes.

Sandbox extras (capability roots, not touched by this runbook):
`aif-example-sandbox` Foundry account (S0, `gpt-4.1-mini` Standard
deployment, pay per token) and `acrexamplesandbox00000000` (ACR Basic,
≈ GBP 2.40 / 30 days).

### 1.3 Databricks workloads (measured 2026-09-23, read-only)

| env | Jobs | Schedules **UNPAUSED** | Apps | Warehouses | All-purpose clusters | Custom serving endpoints | Pipelines |
|---|---|---|---|---|---|---|---|
| sandbox | 0 | 0 | 0 | 1 serverless, STOPPED, auto-stop 10 min | 0 | 0 | 0 |
| dev | 5 | 0 (`ml-lifecycle-demo-monitor-dev` is PAUSED) | 1 `phase19-ml-platform-ops`, compute STOPPED | 1 serverless, STOPPED | 0 | 0 | 0 |
| stg | 6 | 0 (`ml-lifecycle-demo-monitor-stg` is PAUSED) | 0 | 1 serverless, STOPPED | 0 | 0 | 0 |
| prod | 4 | **1 — `ml-lifecycle-demo-monitor-prod` (job 378136892060958), cron `0 0 6 * * ?` UTC, UNPAUSED** | 0 | 1 serverless, STOPPED | 0 | 0 | 0 |

All 16 serving endpoints per workspace are Databricks-managed foundation
models (`system.ai.*`, pay per token). No active runs. The only recurring
spend while idle would be the PROD daily monitor job (≈ GBP 8.9 / 30 days),
which `suspend-workloads.sh prod` pauses.

### 1.4 Foundry hosted agent (Phase 19.1e) — checked 2026-09-24, read-only

Project endpoint
`https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab`
(reachable because the operator IP `198.51.100.252` is on the account's IP
allowlist). Agents in the project:

| Agent | Kind | Versions | Endpoint state |
|---|---|---|---|
| `phase19-hosted-controlled-agent` | **hosted** (container `acrexamplesandbox00000000.azurecr.io/phase19-hosted-controlled-agent:*`, 1 vCPU / 2 GiB) | 1 and 2, both `status: active`; endpoint routes 100 % to `@latest` | `enabled` |
| `platform-engineering-foundry-tool-lab` | prompt (+ tool) | 1 | `enabled` |
| `platform-engineering-foundry-lab` | prompt | 1 | `enabled` |

Is it running and billable? Per the [hosted-agent management
documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent),
the platform provisions container compute when a request arrives and
deprovisions it after the version's idle timeout (default 15 min); there is no
manual start/stop and no container-status API (`.../containers/default`
returns 404 on this project). Billing is by CPU/memory consumption during
active sessions. `status: active` therefore means "ready", not "running". The
measured charge attributed to it was GBP 0.94 (Azure Container Apps meter)
plus GBP 1.06 "Foundry Agents" for the 30 days to 2026-09-22, i.e. the live
proof sessions of early September; with no requests it bills nothing further.

Reversible stop (blocks all requests, keeps agent, versions, identity and
image; **not** run by this runbook — optional):

```bash
BASE_URL="https://aif-example-sandbox.services.ai.azure.com/api/projects/proj-capability-lab"
# disable (reversible)
az rest --method post --resource https://ai.azure.com --url "${BASE_URL}/agents/phase19-hosted-controlled-agent:disable?api-version=v1"
# verify
az rest --method get  --resource https://ai.azure.com --url "${BASE_URL}/agents/phase19-hosted-controlled-agent?api-version=v1" --query '{state:state,configuration_state:configuration_state}'
# re-enable later
az rest --method post --resource https://ai.azure.com --url "${BASE_URL}/agents/phase19-hosted-controlled-agent:enable?api-version=v1"
```

Residual while disabled: none for the agent itself; the ACR image
(Basic registry ≈ GBP 2.40 / 30 days) and the Foundry account/project remain.
Deleting a version or the agent is a separate, explicit decision.

### 1.5 Things that could restart compute or recreate networking

| Source | Trigger | While an environment is idle | Handling |
|---|---|---|---|
| `terraform-cd-platform.yml` | merge to `main` touching `infrastructure/**`, `backend/platform-*.hcl`, `scripts/ci/**`, `Makefile`, the pipeline/template | plans with the **committed** `platform_mode.tf`; guard permits only the six exact deletes | mode is in code; `TF_VAR_idle_mode` is refused |
| `ml-lifecycle-demo-monitor-prod` schedule | daily 06:00 UTC | job cluster starts, cannot egress, fails | paused by `suspend-workloads.sh prod` |
| `databricks-dev-cd.yml` → `databricks-cd-stg.yml` → `databricks-cd-prod.yml` | merge touching `products/hello-databricks/**` or those pipeline files | `bundle deploy` + run | **blocked** by the new platform-mode gate step |
| `ml-lifecycle-demo-dev-cd.yml` → stg → prod | merge touching `products/ml-lifecycle-demo/**` or those pipeline files | `bundle deploy -t prod` would re-apply `monitor_schedule_pause_status: UNPAUSED` | **blocked** by the new platform-mode gate step |
| `products/demand-forecasting` | not deployed anywhere | — | — |
| Azure Automation / Logic Apps / YAML `schedules:` | none exist | — | — |
| Foundry hosted agent | on request only | bills per session | optionally `:disable` (§1.4) |

---

## 2. How idle mode persists, and what CI/CD consumes

**The mode lives in one committed file per environment:**
`infrastructure/environments/<env>/platform_mode.tf`, whose only content is
`variable "idle_mode"` with `default = false` (active) or `default = true`
(idle). This is the file Azure DevOps consumes:

- `terraform-ci-platform.yml` and `terraform-cd-platform.yml` check out the
  exact `main` commit and run `scripts/ci/terraform-plan.sh`, which passes
  only `TF_VAR_subscription_id`, `TF_VAR_resource_group_name` and
  `TF_VAR_storage_account_name` (plus the template's `extraTfVars`, empty for
  every platform root). `idle_mode` is never injected, so Terraform reads the
  committed default.
- `scripts/ci/terraform-plan.sh` and `scripts/ci/terraform-cd-drift-check.sh`
  now **fail if `TF_VAR_idle_mode` is set**, so no pipeline variable, parameter
  or `extraTfVars` entry can flip a mode.
- `*.tfvars` files are git-ignored and do not exist on the hosted agent;
  `terraform.tfvars.example` is documentation only.
- `scripts/ci/check-destructive-plan.sh` reads `variables.idle_mode.value`
  from the plan JSON. Only when it is true, and only for the six exact
  addresses in §1.1 with `actions == ["delete"]`, is a delete permitted;
  replacements, deletes of other addresses (including other resources of the
  same types) and anything when the variable is false or absent fail the run
  (`tests/ci/test-check-destructive-plan.sh`, 22 cases).
- `scripts/ci/check-platform-mode.sh <env>` reads the same
  `platform_mode.tf` line and exits 1 when it says `true`. It runs as a step
  before `bundle deploy` in all six product CD pipelines (`databricks-dev-cd`,
  `databricks-cd-stg`, `databricks-cd-prod`, `ml-lifecycle-demo-{dev,stg,prod}-cd`),
  so a product deployment cannot resume or re-schedule workloads in an idle
  environment.

Consequently, while `platform_mode.tf` says `true` for an environment:
Terraform CD keeps the six resources absent (a plan against a restored
environment would show six deletes, not creates), and product CD fails closed
before deploying. Flipping back to `false` is a reviewed commit.

### What committing, pushing and merging this branch triggers

| Action | Pipelines triggered | Effect on Azure |
|---|---|---|
| `git commit` on `feat/platform-idle-mode` (local) | none | none |
| `git push origin feat/platform-idle-mode` | none — every pipeline has `trigger: none` or `branches: include: main`; feature branches are excluded | none |
| Open a PR to `main` | branch-policy build validation runs the plan-only CI (`terraform-ci-platform.yml`, four read-only plan stages; `terraform-ci.yml` for bootstrap if configured) | read-only |
| **Merge to `main`** | `terraform-cd-platform.yml` (paths `infrastructure/**`, `scripts/ci/**` match) → sandbox, dev, stg **apply automatically** after guard; prod after Environment approval. `databricks-dev-cd.yml` and `ml-lifecycle-demo-dev-cd.yml` also trigger because their own YAML files changed; each runs identity check → **platform-mode gate** → deploy to DEV, then chains to STG and PROD (PROD approval-gated) | Terraform: applies whatever `platform_mode.tf` says at that commit (state moves only if still `false`; the six deletes if `true` and not yet applied locally). Product: redeploys `hello-databricks` and `ml-lifecycle-demo` to DEV/STG/PROD **only if those environments are `false`**; a `true` environment fails its gate step and the chain stops there |

Recommended order (this runbook): apply idle locally per environment **before**
merging, so the CD run on `main` is a no-op for Terraform (guard passes, drift
check 0) and the product pipelines stop at their gates. If you prefer CD to
perform the shutdown, merge with `platform_mode.tf` already flipped and let
the guard-approved deletes run (prod needs its approval click).

---

## 3. Residual cost while idle

Measured baseline (30 days to 2026-09-22): **GBP 177.33** for the subscription.

| Item | Measured / 30 d | Idle | Basis |
|---|---:|---:|---|
| NAT gateways (4) | 96.10 | 0 | measured; removed |
| Private endpoints (8) | 42.39 | 0 | measured; removed |
| PROD monitor job compute | ≈ 8.9 | 0 | measured; schedule paused |
| Public IPs (4, retained) | 10.68 | **≈ 10.7** | measured; Standard static IPs bill unattached |
| Private DNS zones (8, retained) | 2.86 | **≈ 2.9** | measured; unchanged |
| ACR Basic (sandbox) | 2.40 | **≈ 2.4** | measured; unchanged |
| Foundry hosted agent + agents/models | ≈ 2.1 | **0 unless invoked** | measured; per-session billing |
| Databricks Regional meters (dev/stg/prod) | ≈ 9.5 | **unknown, likely lower** | measured; workspace-level charges continue at low volume |
| Storage, Key Vault, Log Analytics | < 0.5 | **< 0.5** | measured; near-zero usage |

Estimate: ≈ GBP 16–30 per month across all four environments while idle.
Estimates are not measured charges; re-query Cost Management after the first
full idle month:

```bash
az rest --method post --url "https://management.azure.com/subscriptions/11111111-1111-1111-1111-111111111111/providers/Microsoft.CostManagement/query?api-version=2023-11-01" \
  --body '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}},"grouping":[{"type":"Dimension","name":"ResourceGroupName"},{"type":"Dimension","name":"MeterCategory"}]}}' \
  --query 'properties.rows' -o table
```

---

## 4. Safety notes before you start

- **Secrets in plans and state.** Saved plans, `terraform show -json` renders
  and state pulls contain the storage accounts' connection strings. Keep them
  only under `tmp/idle/` (git-ignored), never attach them to a PR, and delete
  any `*.json` render after use — Trivy (`make security`) flags the key and
  fails locally while such a file exists. The commands below only render the
  human plan text, which redacts sensitive values.
- **Snapshots.** `capture-workload-state.sh <env> --kind original` writes
  `tmp/idle/<env>/workload-state-original.json` once and refuses to overwrite
  it; later observations are `--kind checkpoint` files. Suspend and restore
  require `--snapshot <path>`, accept only `kind=original`, and verify the
  snapshot's environment and workspace host against the CLI's current
  workspace. A sandbox original was already taken on 2026-09-24T14:59:44Z
  during read-only testing (empty workload set); keep it, or replace it with
  `--force-original` when you start.
- Claude never runs `terraform apply`; every apply below is yours.

---

## 5. Sandbox: complete procedure

### 5.1 Authenticate

```bash
cd ~/src/azure-ai-platform
git status --short --branch | head -1                          # ## feat/platform-idle-mode

az login
az account set --subscription 11111111-1111-1111-1111-111111111111
az account show --query '{sub:name,id:id,user:user.name}' -o json

# Databricks CLI profile (refresh tokens expired on 2026-09-23; this opens a browser)
databricks auth login --host https://adb-1000000000000001.8.azuredatabricks.net --profile aiplatform-sandbox
databricks current-user me --profile aiplatform-sandbox -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])'

export TF_VAR_subscription_id=11111111-1111-1111-1111-111111111111
export TF_VAR_storage_account_name=stexamplesandboxdata
export IDLE_DBX_PROFILE=aiplatform-sandbox
mkdir -p tmp/idle/sandbox

terraform -chdir=infrastructure/environments/sandbox init -reconfigure -input=false \
  -backend-config=../../../backend/platform-sandbox.hcl
```

### 5.2 Capture the original workload state

```bash
# Take (or deliberately replace) the ONE pre-suspension baseline
scripts/idle/capture-workload-state.sh sandbox --kind original --force-original
python3 -c 'import json; s=json.load(open("tmp/idle/sandbox/workload-state-original.json")); print(s["kind"], s["environment"], s["workspace_host"], s["captured_at_utc"])'
```

Expected: `original sandbox https://adb-1000000000000001.8.azuredatabricks.net <timestamp>`,
with 0 jobs, 0 apps, 1 stopped warehouse, 0 clusters, 0 custom endpoints.

### 5.3 Suspend and verify workloads

```bash
scripts/idle/suspend-workloads.sh sandbox --snapshot tmp/idle/sandbox/workload-state-original.json             # dry run
scripts/idle/suspend-workloads.sh sandbox --snapshot tmp/idle/sandbox/workload-state-original.json --execute
scripts/idle/capture-workload-state.sh sandbox --kind checkpoint                                                  # post-suspend proof
python3 - <<'PY'
import glob, json
s = json.load(open(sorted(glob.glob("tmp/idle/sandbox/workload-state-checkpoint-*.json"))[-1]))
bad = [j for j in s["jobs"] if "UNPAUSED" in {j["schedule_pause_status"], j["trigger_pause_status"], j["continuous_pause_status"]}]
bad += [a for a in s["apps"] if a["compute_state"] == "ACTIVE"]
bad += [w for w in s["warehouses"] if w["state"] not in ("STOPPED", "DELETED")]
bad += [c for c in s["clusters"] if c["state"] != "TERMINATED"]
print("workloads quiescent" if not bad else f"STILL ACTIVE: {bad}")
PY
```

Expected for sandbox: "Nothing to suspend" and `workloads quiescent`.

### 5.4 Persist sandbox idle mode (the committed configuration)

```bash
terraform -chdir=infrastructure/environments/sandbox state pull > "tmp/idle/sandbox/state-before-idle-$(date -u +%Y%m%dT%H%M%SZ).tfstate"
chmod 0600 tmp/idle/sandbox/state-before-idle-*.tfstate

sed -i '' 's/^  default     = false$/  default     = true/' infrastructure/environments/sandbox/platform_mode.tf
git diff --stat infrastructure/environments/sandbox/platform_mode.tf            # 1 file, 1 insertion, 1 deletion
grep -n '^  default' infrastructure/environments/sandbox/platform_mode.tf      # default     = true
scripts/ci/check-platform-mode.sh sandbox; echo "gate rc=$? (expected 1 = product deploys blocked)"
```

### 5.5 Fresh saved plan and guard

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ); echo "$TS" > tmp/idle/sandbox/current-ts
terraform -chdir=infrastructure/environments/sandbox plan -input=false -lock-timeout=60s -no-color \
  -out="../../../tmp/idle/sandbox/idle-${TS}.tfplan" | tee "tmp/idle/sandbox/idle-${TS}.plan.txt"
scripts/ci/check-destructive-plan.sh infrastructure/environments/sandbox "../../../tmp/idle/sandbox/idle-${TS}.tfplan"
```

Expected last lines: `Plan: 0 to add, 0 to change, 6 to destroy.` (plus six
`has moved to` lines the first time) and
`==> Only expected idle-mode removals found — guard passed.`

### 5.6 Display the six deletion addresses

```bash
TS=$(cat tmp/idle/sandbox/current-ts)
grep -E '^\s*# .* (will be destroyed|must be replaced|will be updated in-place|will be created)' "tmp/idle/sandbox/idle-${TS}.plan.txt"
```

Expected output, exactly these six lines and nothing else:

```
  # module.network.azurerm_nat_gateway.databricks[0] will be destroyed
  # module.network.azurerm_nat_gateway_public_ip_association.databricks[0] will be destroyed
  # module.network.azurerm_subnet_nat_gateway_association.container[0] will be destroyed
  # module.network.azurerm_subnet_nat_gateway_association.host[0] will be destroyed
  # module.storage_private_access.azurerm_private_endpoint.blob[0] will be destroyed
  # module.storage_private_access.azurerm_private_endpoint.dfs[0] will be destroyed
```

Stop if any other line appears, if any line says `must be replaced` or
`will be updated in-place`, or if the guard did not pass.

### 5.7 Commit the mode, apply the saved plan

```bash
git add infrastructure/environments/sandbox/platform_mode.tf
git commit -m "idle: put sandbox into platform idle mode"

TS=$(cat tmp/idle/sandbox/current-ts)
terraform -chdir=infrastructure/environments/sandbox apply -input=false -lock-timeout=60s \
  "../../../tmp/idle/sandbox/idle-${TS}.tfplan" | tee "tmp/idle/sandbox/idle-${TS}.apply.txt"
```

Expected: `Apply complete! Resources: 0 added, 0 changed, 6 destroyed.`

### 5.8 Verify live networking and preserved resources

```bash
scripts/idle/verify-network-mode.sh sandbox idle
```

This asserts, against live Azure: NAT gateway absent; `pip-dbx-nat-sandbox`
present, still `203.0.113.250`, unattached; both subnets without NAT; both
private endpoints and their NICs absent; zero A records in each private zone;
both zones and both VNet links present; storage still
`publicNetworkAccess=Disabled`/`defaultAction=Deny`/shared key off; workspace,
Access Connector, Key Vault, 3 subnets and NSG associations intact; then
`terraform plan -detailed-exitcode` = 0 and `platform_mode` output = `idle`.
Manual equivalents:

```bash
az network nat gateway show -g rg-aiplatform-sandbox -n nat-dbx-sandbox                       # ResourceNotFound
az network public-ip show -g rg-aiplatform-sandbox -n pip-dbx-nat-sandbox --query '{ip:ipAddress,nat:natGateway}' -o json
az network private-endpoint list -g rg-aiplatform-sandbox --query '[].name' -o tsv             # empty
az network private-dns record-set a list -g rg-aiplatform-sandbox -z privatelink.dfs.core.windows.net  --query 'length(@)'   # 0
az network private-dns record-set a list -g rg-aiplatform-sandbox -z privatelink.blob.core.windows.net --query 'length(@)'   # 0
az network private-dns link vnet list -g rg-aiplatform-sandbox -z privatelink.dfs.core.windows.net  --query '[].name' -o tsv
az network private-dns link vnet list -g rg-aiplatform-sandbox -z privatelink.blob.core.windows.net --query '[].name' -o tsv
az storage account show -n stexamplesandboxdata --query '{pna:publicNetworkAccess,def:networkRuleSet.defaultAction,key:allowSharedKeyAccess}' -o json
az cognitiveservices account show -g rg-aiplatform-sandbox -n aif-example-sandbox --query 'properties.networkAcls.ipRules[].value' -o tsv   # still lists 203.0.113.250
```

### 5.9 Second idle plan: no changes

```bash
terraform -chdir=infrastructure/environments/sandbox plan -input=false -lock-timeout=60s -detailed-exitcode >/dev/null; echo "plan exit=$? (expected 0)"
terraform -chdir=infrastructure/environments/sandbox output platform_mode        # "idle"
terraform -chdir=infrastructure/environments/sandbox output nat_gateway_id       # null
```

### 5.10 Restore plan preview (not applied; committed configuration stays idle)

`-var` overrides are allowed locally (only the CI scripts refuse
`TF_VAR_idle_mode`), so the restore plan is produced against the committed
idle configuration without editing any file:

```bash
TS=$(cat tmp/idle/sandbox/current-ts)
terraform -chdir=infrastructure/environments/sandbox plan -input=false -lock-timeout=60s -no-color \
  -var idle_mode=false -out="../../../tmp/idle/sandbox/restore-preview-${TS}.tfplan" | tee "tmp/idle/sandbox/restore-preview-${TS}.plan.txt"
grep -E '^\s*# .* (will be destroyed|must be replaced|will be updated in-place|will be created)' "tmp/idle/sandbox/restore-preview-${TS}.plan.txt"
scripts/ci/check-destructive-plan.sh infrastructure/environments/sandbox "../../../tmp/idle/sandbox/restore-preview-${TS}.tfplan"
grep -n '^  default' infrastructure/environments/sandbox/platform_mode.tf       # still: default     = true
git status --short infrastructure/environments/sandbox                          # clean
```

Expected: `Plan: 6 to add, 0 to change, 0 to destroy.`, the same six addresses
as `will be created`, guard passes (`delete=0, replace=0`), and
`platform_mode.tf` unchanged. This preview shows Terraform's intent; it does
**not** prove the post-deletion restore path — only the real restore (§7)
does. Do not apply this preview: applying it would leave Azure active while
the committed mode says idle, and CD would delete the resources again.

Clean up renders that could hold secrets (none are produced by the commands
above, but check):

```bash
ls tmp/idle/sandbox/*.json 2>/dev/null | grep -v workload-state || echo "no plan JSON renders present"
```

---

## 6. Repeat for dev, stg, prod

Identical to §5 with these substitutions (the dev/stg/prod roots also refresh
Unity Catalog objects through the Databricks provider, so the profile must be
logged in; `export DATABRICKS_CONFIG_PROFILE=aiplatform-<env>` before the
Terraform commands):

| env | workspace host | `TF_VAR_storage_account_name` | backend |
|---|---|---|---|
| dev | `https://adb-1000000000000002.12.azuredatabricks.net` | `stexampledevdata` | `backend/platform-dev.hcl` |
| stg | `https://adb-1000000000000003.4.azuredatabricks.net` | `stexamplestgdata` | `backend/platform-stg.hcl` |
| prod | `https://adb-1000000000000004.15.azuredatabricks.net` | `stexampleproddata` | `backend/platform-prod.hcl` |

For **prod**, `suspend-workloads.sh prod --execute` pauses job
`378136892060958` (`ml-lifecycle-demo-monitor-prod`); verify with:

```bash
databricks jobs get 378136892060958 --profile aiplatform-prod -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["settings"]["schedule"]["pause_status"])'   # PAUSED
```

For **dev**, the app `phase19-ml-platform-ops` is already STOPPED; the script
leaves it alone.

When all four are applied and verified, push the branch and open the PR
(§2 explains what the merge triggers). Because each environment was applied
locally, the Terraform CD stages plan no changes and the product pipelines
stop at their platform-mode gates.

---

## 7. Restore an environment

```bash
env=sandbox     # or dev/stg/prod, with the matching exports from §5.1 / §6
terraform -chdir=infrastructure/environments/$env init -reconfigure -input=false -backend-config=../../../backend/platform-$env.hcl
terraform -chdir=infrastructure/environments/$env state pull > "tmp/idle/$env/state-before-restore-$(date -u +%Y%m%dT%H%M%SZ).tfstate"

sed -i '' 's/^  default     = true$/  default     = false/' infrastructure/environments/$env/platform_mode.tf
git diff --stat infrastructure/environments/$env/platform_mode.tf                 # 1 insertion, 1 deletion
scripts/ci/check-platform-mode.sh $env                                             # ACTIVE

TS=$(date -u +%Y%m%dT%H%M%SZ)
terraform -chdir=infrastructure/environments/$env plan -input=false -lock-timeout=60s -no-color \
  -out="../../../tmp/idle/$env/restore-${TS}.tfplan" | tee "tmp/idle/$env/restore-${TS}.plan.txt"
grep -E '^\s*# .* (will be destroyed|must be replaced|will be updated in-place|will be created)' "tmp/idle/$env/restore-${TS}.plan.txt"
scripts/ci/check-destructive-plan.sh infrastructure/environments/$env "../../../tmp/idle/$env/restore-${TS}.tfplan"
```

Gate: `Plan: 6 to add, 0 to change, 0 to destroy.`, six `will be created`
lines for the §1.1 addresses, guard `delete=0, replace=0`.

```bash
git add infrastructure/environments/$env/platform_mode.tf
git commit -m "restore: return $env to platform active mode"
terraform -chdir=infrastructure/environments/$env apply -input=false -lock-timeout=60s "../../../tmp/idle/$env/restore-${TS}.tfplan"

scripts/idle/verify-network-mode.sh $env active
# DNS re-registered by the zone groups, endpoints approved, egress IP unchanged:
az network private-dns record-set a list -g rg-aiplatform-$env -z privatelink.dfs.core.windows.net  -o table
az network private-dns record-set a list -g rg-aiplatform-$env -z privatelink.blob.core.windows.net -o table
az storage account show -n stexample${env}data --query 'privateEndpointConnections[].{pe:privateEndpoint.id,status:privateLinkServiceConnectionState.status}' -o table
az network public-ip show -g rg-aiplatform-$env -n pip-dbx-nat-$env --query ipAddress -o tsv

# Workloads: only what the ORIGINAL snapshot recorded as active
scripts/idle/restore-workloads.sh $env --snapshot tmp/idle/$env/workload-state-original.json
scripts/idle/restore-workloads.sh $env --snapshot tmp/idle/$env/workload-state-original.json --execute
```

End-to-end data-path proof for dev/stg/prod: run `hello-databricks-<env>` once
(dev 469299606674766, stg 257303166888448, prod 975037852280521), which starts
a job cluster in the injected VNet (needs the NAT) and reads/writes the
environment catalog (needs the private endpoints):

```bash
databricks jobs run-now <job id> --profile aiplatform-$env
```

Then merge the flip to `main`; creates pass the guard and the product gates
open again.

---

## 8. Recoverability checks performed (read-only, 2026-09-23/24)

| Check | Result |
|---|---|
| Backend `stexampletf000000`: `terraform init` for all four roots from this machine | OK |
| Provider refresh (azurerm, azapi, databricks) for all four roots | OK — baseline plans `No changes.` |
| Active configuration with the flag | 0/0/0 + 6 moves, all four environments |
| Idle plans (saved under `tmp/idle/<env>/idle.tfplan`) | exactly 6 deletes, all four; exact-address guard passes; guard fails when the variable is absent |
| Guard unit tests / promotion-gate tests | 22/22 and existing suite pass |
| Platform-mode gate | ACTIVE for all four; returns 1 when `platform_mode.tf` is flipped |
| Snapshot scripts (sandbox, live read-only) | original written once and protected; checkpoint written; suspend/restore dry runs; refusal of checkpoint snapshots and of a sandbox snapshot against dev |
| `verify-network-mode.sh sandbox active --skip-plan` | PASS |
| Foundry hosted agent | 2 active versions, endpoint enabled, no running container API; reversible `:disable` prepared, not run |

**Not proven:** the post-deletion restore path; the first real restore is the
proof.

## 9. Blocked or not performed

- Databricks CLI profiles had expired refresh tokens; read-only inventory and
  script tests used `IDLE_DBX_AUTH=azure-cli`. Re-login before §5.1.
- No `terraform apply`, no Databricks or Foundry write, no Azure change.
- The Foundry hosted agent's container state cannot be read (no API); billing
  evidence is from Cost Management only.
