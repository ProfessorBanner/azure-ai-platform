# ADR 0013: Reversible platform idle mode

## Status

Accepted — 2026-09-23. (ADR 0012 is reserved by the in-flight `aks-mlops`
cost-controls branch; this record is numbered 0013 to avoid a collision.)

## Context

The four platform environments (`sandbox`, `dev`, `stg`, `prod`) will not be
used for some time. Measured spend over the 30 days to 2026-09-22 was
GBP 177.33 for the whole subscription, and the continuously billed networking
resources dominate it:

| Meter (all four environments) | GBP / 30 days | Share |
|---|---:|---:|
| NAT Gateway (4 × `nat-dbx-<env>`) | 96.10 | 54 % |
| Virtual Network Private Link (8 storage private endpoints) | 42.39 | 24 % |
| IP Addresses (4 × `pip-dbx-nat-<env>`) | 10.68 | 6 % |
| Azure DNS (8 private DNS zones) | 2.86 | 2 % |
| Everything else (Databricks compute for the PROD monitor job, managed-RG disks, ACR, Foundry, Container Apps) | 25.30 | 14 % |

The NAT gateways exist only to give VNet-injected Databricks compute (Secure
Cluster Connectivity, no public IPs) deterministic egress; the private
endpoints exist only so that compute can reach the default-deny ADLS Gen2
accounts. Neither does anything while no compute runs. Everything else must
survive untouched: data, models, Unity Catalog, Key Vault contents, identities,
the Terraform backends and the ability to restore each environment exactly.

Constraints from the existing platform (ADR 0002, 0003, 0005):

- one Terraform state per environment; environment-neutral modules; a stable
  root output contract;
- Terraform CD applies only reviewed plans and fails on any delete or replace;
- no long-lived credentials; no portal-only configuration;
- `terraform destroy`, resource-group deletion and state surgery are not
  acceptable tools.

## Decision

### A committed, per-environment mode switch

Each environment root declares `variable "idle_mode"` (bool, default `false`)
in a dedicated one-purpose file, `infrastructure/environments/<env>/platform_mode.tf`.
**The committed default is the environment's mode.** Terraform CI plans, CD
applies and the post-apply drift check all evaluate it from the checked-out
`main` commit; the CI scripts refuse to run if `TF_VAR_idle_mode` is set, so
no pipeline parameter can override it. Local `-var idle_mode=...` overrides are
for producing review/preview plans only; before any apply the default is
flipped and committed, and a second plan with no override must show no changes.

Product deployments read the same file: `scripts/ci/check-platform-mode.sh
<env>` runs before `bundle deploy` in every product CD pipeline and fails
closed when the environment is idle, so a deployment cannot resume compute or
re-enable an `UNPAUSED` bundle schedule while networking is removed.

### Idle scope is exactly six resources per environment

`idle_mode = true` sets `nat_gateway_enabled = false` on `module.network` and
`private_endpoints_enabled = false` on `module.storage_private_access`, which
gives `count = 0` to, and therefore destroys:

| Terraform address | Azure resource |
|---|---|
| `module.network.azurerm_nat_gateway.databricks[0]` | `nat-dbx-<env>` |
| `module.network.azurerm_nat_gateway_public_ip_association.databricks[0]` | NAT ↔ `pip-dbx-nat-<env>` link |
| `module.network.azurerm_subnet_nat_gateway_association.host[0]` | NAT ↔ `snet-dbx-host-<env>` |
| `module.network.azurerm_subnet_nat_gateway_association.container[0]` | NAT ↔ `snet-dbx-container-<env>` |
| `module.storage_private_access.azurerm_private_endpoint.dfs[0]` | `pe-stexample<env>data-dfs` (+ NIC, zone group, A record) |
| `module.storage_private_access.azurerm_private_endpoint.blob[0]` | `pe-stexample<env>data-blob` (+ NIC, zone group, A record) |

Retained in both modes: the NAT **public IP resource and its address**
(external allowlists such as the sandbox Foundry account's IP rules reference
`203.0.113.250`), VNet, subnets and delegations, NSG and associations, the two
private DNS zones and their VNet links, storage accounts and data, Key Vault,
Log Analytics, Databricks workspaces, Access Connectors, Unity Catalog objects,
role assignments, identities, resource groups and the Terraform backend.
Storage `publicNetworkAccess = Disabled` / `defaultAction = Deny` is **not**
relaxed.

DNS correctness: the A records are owned by each endpoint's
`private_dns_zone_group`; deleting the endpoint deletes the group and Azure
removes its records, so no stale endpoint IP survives in the retained zones.
Restoration recreates the endpoints and the zone groups re-register fresh
records. `scripts/idle/verify-network-mode.sh` checks both directions.

### Introducing the switch is a pure state move

The six resources were unconditional when deployed. Adding `count` changes
their addresses to `[0]`; `moved` blocks inside both modules carry the state
across, so the active configuration plans as `0 to add, 0 to change, 0 to
destroy` with six moves. This was verified against live state in all four
environments before the change was proposed.

### The CD guard follows the mode, and only the mode

`scripts/ci/check-destructive-plan.sh` reads `variables.idle_mode.value` from
the plan JSON. When it is true, a change is permitted only if its address is
one of the six exact addresses above **and** its action list is exactly
`["delete"]`. Every other delete — another resource of the same type, the same
resource at another index or without an index, anything in another module —
and every replacement still fail the run. When the variable is false or absent
the guard is unchanged. `tests/ci/test-check-destructive-plan.sh` (22 cases)
proves this table.

### Workload suspension is scripted, snapshot-driven and reversible

Networking is only half of "idle". Under `scripts/idle/`:

- `capture-workload-state.sh` records jobs (schedule/trigger/continuous
  `pause_status`), apps, warehouses, all-purpose clusters, serving endpoints,
  pipelines and active runs to a git-ignored snapshot;
- `suspend-workloads.sh` pauses only what was unpaused, cancels active runs,
  stops apps, warehouses and clusters — dry-run by default, never deletes;
- `restore-workloads.sh` unpauses only what the snapshot recorded as unpaused
  and starts only apps that were running.

Custom model-serving endpoints have no reversible stop; the scripts report
them and leave deletion as a separate, explicit decision. At the time of this
decision no custom endpoint exists — every endpoint in the four workspaces is
a Databricks-managed, pay-per-token foundation model.

### Why not the alternatives

- **Delete the public IPs too** (≈ GBP 2.67 per environment per month): saves
  little and breaks the sandbox Foundry allowlist and any future allowlist
  that pins the egress address. Rejected.
- **Delete the private DNS zones and links** (≈ GBP 0.72 per environment per
  month): zones are global, cheap, and the record lifecycle is already handled
  by the zone groups. Deleting them would add a restore ordering dependency
  for no material saving. Rejected.
- **Relax storage networking instead of removing endpoints**: changes the
  security posture the requirement says must stay unchanged. Rejected.
- **`terraform destroy -target` or state removal**: unreviewable, not
  persisted in code, and CD would recreate the resources on its next run.
  Rejected.
- **A pipeline parameter instead of a committed variable**: the mode would
  live outside the reviewed commit and the guard could not see it in the plan.
  Rejected.

## Consequences

### Positive

- Expected saving ≈ GBP 37 per environment per month (NAT gateway ≈ 24,
  private endpoints ≈ 10.6, plus the daily PROD monitor's compute once its
  schedule is paused); ≈ GBP 150 per month across the four environments,
  against a measured GBP 177 baseline. Residual per environment ≈ GBP 3.4
  (public IP 2.67 + DNS 0.72) plus storage, Log Analytics and Key Vault at
  their current near-zero usage; sandbox additionally keeps the Basic ACR
  (≈ GBP 2.40) and the Foundry account (pay per token).
- Restoration is a one-line code change per environment followed by the same
  plan → guard → apply → drift-check path as every other change.
- The mode is visible in code (`idle_mode` default), in Terraform outputs
  (`platform_mode`) and in the CD plan summary.

### Negative / accepted

- While idle, Databricks classic compute in an environment has no egress and
  no data-lake path. Anything that starts compute before restoration (a
  manual run, an unpaused schedule, a bundle redeploy with an `UNPAUSED`
  schedule) will fail rather than cost money — but it will fail.
- A `databricks bundle deploy` of `ml-lifecycle-demo` or `demand-forecasting`
  to a target whose bundle variables say `UNPAUSED` re-enables that schedule.
  The product CD pipelines only run on merges touching those products, so the
  runbook's rule is: do not merge product changes for an idle environment, or
  pause again after deploying.
- After this ADR's code is merged, each environment's first CD run applies
  the six state moves (plan shows moves, zero add/change/destroy); until that
  apply the drift check reports pending changes for that environment.
- The Foundry hosted agent `phase19-hosted-controlled-agent` (two `active`
  versions, endpoint `enabled`) is not Terraform-managed. Foundry provisions
  its container per request and deprovisions after the idle timeout, so it
  bills nothing without traffic; the reversible `:disable` call is documented
  in the runbook and left as an operator decision.
- Merging this change triggers the two product DEV pipelines (their YAML
  changed); they redeploy `hello-databricks` and `ml-lifecycle-demo` through
  DEV → STG → PROD only for environments whose mode is active, and stop at
  the gate for idle ones.
