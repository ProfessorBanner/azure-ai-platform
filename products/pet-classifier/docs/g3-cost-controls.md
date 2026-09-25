# G3 — Azure deployment preparation and cost controls

G3 prepares the Azure side of the Pet Image Classifier **without creating,
modifying or deleting a single Azure resource**. It delivers the code,
Terraform, offline tests, read-only discovery, price evidence and the
operating procedure that G4 needs before any GPU compute is provisioned.

The whole project's Azure budget is **USD 100 total** (not per month). The
design targets **USD 60 of planned exposure** and keeps **USD 40 of
contingency** that ordinary session admission can never reach. Nothing here
claims that spending is technically capped at USD 100: Azure bills what runs,
deletion has latency, and a watchdog is a bounded control, not a billing cap.

```
laptop (.local/g3, private)            Azure (nothing exists yet)
  discovery/   read-only preflight       rg-...-controls   watchdog + identity   (control)
  prices/      retail evidence + hash    rg-...-retained   inputs, models, results (retained)
  ledger/      lifetime ledger + lock    rg-...-session    AKS + ACR, EMPTY shell  (disposable)
  sessions/<id>/ estimate, binding       rg-...-session-nodes  AKS-managed        (disposable)
```

## Git state and inherited work

| Item | State |
|---|---|
| Base commit | `52d9074` Phase G1 (the `feat/g1-pet-classifier-local` head) |
| G3 branch | `feat/g3-pet-classifier-cost-controls`, created from that commit |
| G2 work | **Uncommitted**, inherited in the working tree unchanged: `Dockerfile`, `.dockerignore`, `deploy/helm/`, `deploy/jobs/`, `scripts/g2/`, `docs/g2-local-kubernetes.md`, `tests/test_g2_scripts.py`, plus G1 corrections in `artifacts.py`, `config.py`, `model.py`, `train.py`, `pyproject.toml`, `uv.lock`, `.gitignore`, `README.md`. G3 does not commit them. |
| Unrelated uncommitted work | Root `pyproject.toml`/`uv.lock` (adds `mlflow`), `products/chainanalysis/`, `products/demand-forecasting/`, `products/data_engineering_create_features.py`, `.local/`. Untouched and not committed. |
| G3 files | Only the paths listed under "What was added" are staged for the G3 commit. |

## Finding: the G2 image cannot be the G4 training image

- `pyproject.toml` (G2) pins torch/torchvision to PyTorch's **CPU-only** index
  for every Linux platform, so the G2 image **cannot run CUDA training**.
- The G2 image was built and tested on **linux/arm64** (Apple Silicon host).
- The proposed GPU node, `Standard_NC4as_T4_v3`, is **x86-64** (discovery:
  `CpuArchitectureType x64`); so is the system node `Standard_D4als_v6`.
- Therefore G4 needs a **separately reproducible linux/amd64 CUDA training
  image** with its own lockfile resolution (CUDA wheels), built for amd64 and
  pushed to the session registry. G3 does **not** rebuild the GPU stack; the
  serving/MLflow image can stay CPU-only.

## Architecture and lifecycle decisions

| Class | Resource group | Contents | Who deletes it | Cost class |
|---|---|---|---|---|
| Control | `rg-aiplatform-aksmlops-controls` | Automation account (system-assigned MI), PowerShell 7.4 runtime environment, runbook, hourly sweep schedule, per-session one-time schedule, allowlist variables, custom delete-only role | Only a reviewed `terraform destroy` of the controls root, never the watchdog | pennies (Automation minutes) |
| Retained | `rg-aiplatform-aksmlops-retained` | One Standard LRS storage account (no shared keys, Entra only): `inputs`, `models`, `watchdog-results`; lifecycle rule deletes blobs 60 days after last modification | Lifecycle rule + reviewed destroy | USD 0.0192/GB-month |
| Disposable | `rg-aiplatform-aksmlops-session` | **Empty shell created by the controls root**; the session root deploys AKS (Free tier), one `Standard_D4als_v6` system node, optional single T4 node (disabled initially), a Basic ACR for transport | The watchdog (report-only until live-verified), or a session-root destroy | the session estimate |
| AKS-managed | `rg-aiplatform-aksmlops-session-nodes` | VMSS, OS disks, Standard LB, one public IP, NSG, route table | AKS, when the cluster is deleted; the watchdog re-inventories it | inside the session estimate |

Why the session group is created empty by the controls root: the watchdog's
role assignment must be scoped to that exact group **before** compute exists.
This removes the cycle "cleanup protection cannot be configured until the GPU
cluster exists". The session root references the group by name and never
creates or destroys it; an apply fails fast if it is missing.

Networking: AKS needs an outbound path. The `none` outbound type is only
available with a network-isolated cluster, so the design keeps the default
**Standard Load Balancer with one managed outbound public IP** and prices it
(USD 0.025/h rules, USD 0.005/GB processed, USD 0.005/h IP). Port-forward
removes *ingress*, not this egress infrastructure. No ingress, NodePort,
NAT gateway, monitoring add-on, managed database or private networking.

Network exposure: the retained storage account is **default-deny** with a
runtime-supplied IP allow-list (operator egress; G4 adds the session
cluster's outbound IP before training pulls inputs), and the AKS API server
only answers **authorized IP ranges** (operator egress; AKS adds its own
egress IP). Because Azure Automation is not a trusted service and its
sandbox addresses cannot be allow-listed, the watchdog's result-blob write is
best-effort; the Automation job history (30 days) is the authoritative
execution record. The Basic ACR endpoint is public and RBAC-gated (Basic has
no network rules): an explicit decision.

Identity: Entra only. AKS local accounts are disabled and Azure RBAC is on;
the operator is granted "Azure Kubernetes Service RBAC Cluster Admin" on the
cluster and `AcrPush` on the registry; the kubelet identity gets `AcrPull` and
"Storage Blob Data Reader" on the retained account. No secrets, tokens or
keys exist anywhere in the design. The caller is subscription Owner today;
the watchdog identity is not: it holds a custom role
(`*/read` + delete on managed clusters, registries, user-assigned identities,
deployments) scoped to the session group, `Reader` at subscription scope
(read-only, justified by re-inventory of the AKS node group that cannot be a
scope before it exists) and blob write on the `watchdog-results` container.

Image transport: a **Basic ACR in the session group** (USD 0.1666/day retail
plus USD 0.10/GB-month) is the least expensive workable transport; it is
created and deleted with the session, so its lifetime is measured in hours.
Alternatives considered: the existing sandbox registry (rejected: the brief
forbids reusing platform resources), a public registry (USD 0 but the image
would be public), or importing a tarball into containerd (fragile on AKS).

Storage for G4 (the G2 hostPath design is local-only): the operator uploads
the immutable inputs (dataset manifest + images, ImageNet weights) to
`inputs/` in the retained account **before** compute exists; training Pods
read them with the kubelet identity; the export step copies the model package
to `models/<run_id>/` **before normal teardown**. Emergency expiry does not
wait for an export: an un-exported package is lost by design, which is why
export happens during the session and not at the end.

## Read-only discovery findings (UK South, 2026-09-17)

Recorded by `scripts/g3/discover.py` into `.local/g3/discovery/` (private;
contains subscription and tenant ids, never committed). Summary:

| Check | Finding |
|---|---|
| Subscription / tenant | Recorded (single subscription; explicit `--subscription` on every call) |
| Resource providers | ContainerService, Compute, Network, Automation, ContainerRegistry, Storage, ManagedIdentity, Authorization, CostManagement, Consumption, Insights, OperationalInsights: all **Registered** |
| `Standard_NC4as_T4_v3` | Listed in UK South, no restrictions, x64, 4 vCPU / 28 GiB / 1 GPU, zones 1-3 |
| `Standard_D4als_v6` | Listed, no restrictions, x64, 4 vCPU / 8 GiB; family `standardDalv6Family` quota **0/10 vCPU** |
| **GPU quota** | family `Standard NCASv3_T4 Family` **0/0 vCPU → BLOCKER**. A quota request is a G4 human action; G3 does not request it |
| Dasv5 / Dsv5 families | 0/0 vCPU: `D4as_v5` and `D4s_v5` are NOT usable; that is why D4als_v6 was chosen |
| Regional total vCPU | 0/32 |
| AKS versions | 1.34, **1.35 (default)**, 1.36 supported (KubernetesOfficial); pinned 1.35.7 |
| System-pool rules (docs, 2026-08) | at least 4 vCPU and 4 GB, no B-series, Linux; production wants 2+ nodes. **One node is a documented lab deviation**; `system_node_count` allows 2 if AKS refuses 1 |
| GPU on AKS (docs, 2026-07) | `Standard_NC4as_T4_v3` is the documented minimum GPU size; AKS-managed driver install on Ubuntu; `--gpu-driver` field replaces the retired skip tag |
| Caller permissions | subscription **Owner** (sufficient; broader than least privilege, noted) |
| Name collisions | none of the four planned groups exist; existing `acrexamplesandbox00000000` (Basic) is NOT reused |
| Cost Management | query API returned **429 Too Many Requests** during discovery → recorded as "unavailable now"; billing currency from usage details is **GBP**, pricing currency USD |
| Automation docs | one-time and recurring schedules; **most frequent recurrence is one hour**; PowerShell 7.4 runtime with Az 12.3.0 default; **fair share stops jobs after 3 h**; job history kept 30 days; 500 free minutes/month (not assumed) |

Quota is not capacity: a successful node-pool creation is the only proof of
capacity, and G3 makes none.

## Price evidence (Azure Retail Prices API, USD, retrieved 2026-09-17)

`scripts/g3/prices.py` follows pagination, selects exactly one on-demand
Linux record per meter (Windows, Spot, Low Priority, DevTest and reservation
records excluded), refuses zero or multiple matches, stores raw records plus a
normalised view and hashes the result (`f378deed6f8c…` at the time of writing).

| Key | Meter | Unit price |
|---|---|---|
| vm_cpu_hour | D4als v6 (Linux) | 0.187 / hour |
| vm_gpu_hour | NC4as T4 v3 (Linux) | 0.615 / hour |
| os_disk_p6_month | P6 LRS Disk | 12.3499 / month |
| lb_rules_hour | Standard Included LB Rules and Outbound Rules (published as region Global) | 0.025 / hour |
| lb_data_gb | Standard Data Processed | 0.005 / GB |
| public_ip_hour | Standard IPv4 Static Public IP | 0.005 / hour |
| egress_gb | Standard Data Transfer Out, dearest tier, no free allowance | 0.087 / GB |
| acr_basic_day | Basic Registry Unit | 0.1666 / day |
| acr_storage_gb_month | Data Stored | 0.10 / GB-month |
| automation_minute | Process Automation Basic Runtime, no free minutes assumed | 0.002 / minute |
| blob_hot_gb_month | Hot LRS Data Stored, dearest tier | 0.0192 / GB-month |

AKS Free tier control plane: no charge (verified in the Free/Standard tier
documentation; the Standard uptime SLA meter is USD 0.10/h and is not used).

Currency: the subscription **bills in GBP**. Reservations stay in USD with an
explicit **5 % FX allowance**; taxes are excluded from retail prices and are
reserved as an explicit **20 % allowance**. Reconciled GBP amounts are
converted with the invoice rate and recorded as such; GBP is never treated as
USD. Public retail prices are estimates, not account-specific charges.

## Budget policy v1 (`deploy/g3/budget-policy.v1.json`)

| Parameter | Value |
|---|---|
| project_limit_usd / admission_limit_usd / contingency_usd | 100 / 60 / 40 |
| max concurrent sessions / max GPU nodes | 1 / 1 |
| active session target | 120 minutes |
| training Job deadline | 1800 seconds |
| allowances (minutes) | provisioning 20, image pull 10, deletion delay 20, watchdog delay 60 |
| expiry | reservation time + 170 min (target + provisioning + pull + deletion delay); the watchdog delay is charged for but never moves the deadline |
| bounds | egress 10 GB, LB data 20 GB, registry 1 day + 3 GB, watchdog 60 min, retained 10 GB for 1 month |
| retention | retained blobs expire 60 days after last modification |
| evidence freshness | prices 72 h, discovery 72 h, watchdog verification 30 days |

The same numbers are validated by the Terraform `budget_policy` variable.
Allowances are planning margins, not guaranteed upper bounds on Azure billing
or deletion latency.

## Example session estimate and ledger calculation

`session.py estimate --config deploy/g3/session-config.example.json` (one
D4als_v6 system node, one T4 node, 64 GB P6 disks), 2026-09-17 evidence:

```
billable hours (target + allowances, rounded up): 4      # ceil(230 / 60)
  cpu_compute            4 node-hour x 0.187    = 0.75
  gpu_compute            4 node-hour x 0.615    = 2.46
  os_disks               8 disk-hour x 0.016918 = 0.14   # 12.3499 / 730, rounded up
  load_balancer_rules    4 hour      x 0.025    = 0.10
  load_balancer_data    20 GB        x 0.005    = 0.10
  public_ip              4 hour      x 0.005    = 0.02
  egress                10 GB        x 0.087    = 0.87
  registry               1 day       x 0.1666   = 0.17
  registry_storage       3 GB-month  x 0.1      = 0.30
  watchdog              60 minute    x 0.002    = 0.12
  retained_storage      10 GB-month  x 0.0192   = 0.20
  subtotal                                        5.22
  fx allowance (5 %)                              0.27
  tax allowance (20 %)                            1.05
  RESERVATION (USD, rounded up)                   6.52
```

Lifetime ledger arithmetic (`ledger.py`): each non-cancelled session counts
exactly once as `actual` if settled (complete coverage **and** verified
teardown) else `max(reservation, actual so far)`; plus the retained reserve.
Admission projects `committed + new reservation` against USD 60:

| Ledger state | Committed | + reservation | Projected | Cost fits |
|---|---|---|---|---|
| empty (today) | 0 | 6.52 | 6.52 | yes (headroom 53.48) |
| eight settled sessions at 6.52 | 52.16 | 6.52 | 58.68 | yes |
| settled 53.48 | 53.48 | 6.52 | **60.00** | yes (equality fits) |
| settled 53.49 | 53.49 | 6.52 | 60.01 | **no**; contingency not available |
| one session ended, teardown unverified, billing empty | 6.52 (retained) | 6.52 | 13.04 | cost yes, **execution blocked** (unresolved) |

Today's dry run reports **cost fits: YES** and, separately,
**cloud execution: BLOCKED** for three reasons: the T4 quota blocker, quota
not verified, and "watchdog not live-verified". `reserve` refuses for the
same reasons; there is no bypass flag.

## Expiry watchdog (Azure Automation)

One concrete external design, in the controls root:

- **Account**: `aa-aiplatform-aksmlops-watchdog`, Basic SKU, system-assigned
  managed identity, local authentication disabled, public network access off.
- **Runtime**: PowerShell 7.4 runtime environment with the default Az 12.3.0
  modules (documented as supported for cloud jobs in all regions).
- **Runbook**: `Invoke-SessionExpiry` (`controls/runbooks/Invoke-SessionExpiry.ps1`).
  Parameters `Mode` (Report default / Execute), `Trigger`, `SessionId`.
- **Schedules**: one **hourly sweep** (the most frequent supported cadence)
  and one **one-time schedule per armed session** whose start time *is* the
  session expiry; both call the same runbook. Arming is a Terraform change of
  the `armed_session` variable and is stage 3 of the G4 order, before compute.
- **Allowlist**: Automation variables set by Terraform (subscription id,
  project, session group id, node group id, armed session id and expiry,
  results container). A schedule parameter must agree with them.
- **Eligibility** (all required): exact subscription; exact allowlisted
  group id; group tags `project`, `session_id`, `expiry_utc`,
  `lifecycle=disposable` matching the armed values; expiry reached; each
  resource tagged `lifecycle=disposable` with the same `session_id`. No
  prefix scanning, no wildcard scopes, no subscription-wide Contributor.
  Control and retained resources live in other groups and are never listed.
- **Execution**: managed clusters first (their deletion removes the node
  group); idempotent (missing = done, accepted ≠ done); polls each deletion
  up to 40 minutes; at most 3 attempts; 75-minute job budget (under the 3 h
  fair share); re-inventories the session group and the node group; reports
  residuals as `unresolved` and **throws**, so a failed cleanup is a failed
  job, never a success.
- **Durable results**: job history (30 days) plus one JSON result blob per
  run in `watchdog-results`, written with the managed identity.
- **Survives**: closed laptop, disconnected CLI, failed AKS control plane,
  missing Pods, because nothing runs on the cluster or the laptop.
- **Not a hard cap**: a stuck ARM deletion is reported, not hidden; billing
  continues until Azure completes the delete.

`scripts/g3/watchdog.py` is a Python reference model of the same rules with
a fake-client test suite and a **report-only** local runner; its execute path
requires both `--execute` and `G3_ALLOW_CLOUD_MUTATION=1` and is never used
in G3. **Status: written and reviewed offline, NOT live-verified.** The
runbook has not executed in Azure; G4 verifies it first (checklist below).

## Terraform

Two independent roots and states under `infrastructure/capabilities/aks-mlops/`:

| Root | State key | Contents | Real plan (2026-09-17) |
|---|---|---|---|
| `controls/` | `capabilities/aks-mlops/controls.tfstate` | 3 resource groups, Automation account + runtime environment + runbook + schedules + 8 variables, custom role, 3 role assignments, retained storage account + 3 containers + lifecycle policy, optional subscription budget | **25 to add, 0 change, 0 destroy** |
| `session/` | `capabilities/aks-mlops/session.tfstate` | AKS (Free, 1 system node), optional GPU pool, Basic ACR, 4 role assignments | **6 to add** (GPU disabled), **7 to add** (GPU enabled), 0 destroy |

Both pin `azurerm ~> 4.0` (locked at 4.81.0 like the rest of the repository)
and set `resource_provider_registrations = "none"` so a plan can never
register a provider as a side effect. Input validation covers subscription
GUID, region (uksouth/ukwest), session id, RFC 3339 UTC expiry, the SKU and
count allowlists and the budget policy arithmetic. Backends: partial config
in `backend/capability-aks-mlops-{controls,session}.hcl` (bootstrap storage,
new keys; no state blob exists yet).

How the plans were produced: `terraform init -reconfigure` with a temporary
`plan_override.tf` (git-ignored by the `*_override.tf` rule) pointing the
backend at a scratch local path, then `terraform plan -lock=false -out` with
the real subscription id, the operator object id, an example session id and
the derived retained-account id, and RFC 5737 placeholders for the IP
allow-lists. The plans called live Azure APIs only for
provider/client configuration; they were written to the scratch directory,
never to the repository, and were **not applied**. The override files were
removed afterwards. Mocked Terraform tests: none. Render-only examples: the
`terraform.tfvars.example` files.

Staged deployment order for G4 (each stage is a separate reviewed apply):

1. Controls root with `armed_session = null`, `watchdog_mode = "Report"` →
   groups, identity, retained storage, hourly sweep.
2. Prove the watchdog: start the runbook manually in Report mode against the
   empty session group; then arm a **throw-away** session id with an expiry a
   few minutes out, put a tagged, cost-free test resource (a user-assigned
   identity) in the session group, switch `watchdog_mode = "Execute"`, and
   confirm the one-time job deletes it, the sweep reports "already-clean",
   and the result blob exists. Record it with `session.py watchdog-verified`.
3. Arm the real session (`session.py reserve` → `armed_session`), apply the
   controls root; the one-time deadline exists before any compute.
4. Session root apply of the **bound** plan (`session.py bind`,
   `verify-binding`, `deploy_session.sh`), GPU pool still disabled; then a
   second bound plan with the pool enabled once quota exists.

Binding: `session.py bind` records the hashes of the config, price evidence,
estimate and policy plus the SHA-256 of the plan JSON and its resource-change
list (deletions refused). `verify-binding` fails if any of them changed, so a
different plan cannot be applied under an old estimate.

## Budget alerts (optional, supplementary)

`controls/budget.tf` defines a monthly subscription-scope budget of the
admission limit filtered to the four groups **including the AKS node group**
(a group-scoped budget cannot precede the group). Disabled by default because
it needs a real recipient; `budget_alert.contact_emails` is supplied at
runtime, never committed. Its monthly reset is distinct from the lifetime
ledger, and it cannot stop spend.

## Tests and validation results

| Check | Result |
|---|---|
| `uv run ruff format --check .` / `ruff check` (product) | clean |
| `uv run mypy scripts/g3 tests/test_g3_*.py` (strict) | clean |
| `uv run pytest` (product) | **125 passed** (75 inherited G1/G2 + 50 new G3) |
| `terraform fmt -check -recursive`, `terraform validate` (both roots) | clean |
| `tflint --recursive` (aks-mlops) | clean |
| `trivy fs --scanners secret,misconfig --skip-dirs '**/.terraform'` (aks-mlops, scripts/g3, deploy/g3, backend) | clean after adding NetworkPolicy, storage default-deny and API-server authorized ranges |
| Real `terraform plan` | controls 25/0/0, session 6/0/0 and 7/0/0 (see above) |

New tests (`tests/test_g3_*.py`, fake clients only, no Azure calls):
Decimal arithmetic and round-up; equality at USD 60 fits and USD 60.01
blocks; missing, stale, ambiguous and wrong-currency evidence rejected;
concurrent session refused; unresolved reservation retained after a session
ends; empty billing does not release a reservation; idempotent reconciliation
without double counting; actual above reservation uses actual; partial
teardown stays unresolved; wrong subscription / group / project / session
never eligible; not-expired vs expired with an injected clock; already
deleted is safe; failed deletion remains unresolved; foreign-tagged resources
left in place; changed configuration invalidates admission; real compute
blocked before live watchdog verification; local execute refused without the
environment gate.

## Manual commands (from `products/pet-classifier/`)

```bash
export G3_SUBSCRIPTION_ID=<subscription id>            # explicit; the CLI default is never used

# Read-only discovery and price evidence (private files under .local/g3/)
uv run python scripts/g3/discover.py --region uksouth
uv run python scripts/g3/prices.py   --region uksouth

# Estimate, dry-run admission, ledger status
uv run python scripts/g3/session.py estimate --config deploy/g3/session-config.example.json
uv run python scripts/g3/session.py admit    --config deploy/g3/session-config.example.json
uv run python scripts/g3/session.py status

# Terraform (from the repository root): format, validate, lint, real plan (no apply)
terraform fmt -check -recursive infrastructure/capabilities/aks-mlops
terraform -chdir=infrastructure/capabilities/aks-mlops/controls init -backend=false && terraform -chdir=infrastructure/capabilities/aks-mlops/controls validate
terraform -chdir=infrastructure/capabilities/aks-mlops/session  init -backend=false && terraform -chdir=infrastructure/capabilities/aks-mlops/session  validate
tflint --recursive --chdir infrastructure/capabilities/aks-mlops
# real plan (G4, with the azurerm backend): terraform init -backend-config=backend/capability-aks-mlops-controls.hcl && terraform plan -var-file=terraform.tfvars

# Tests
uv run pytest -q tests/test_g3_policy_estimate.py tests/test_g3_prices.py tests/test_g3_ledger_admission.py tests/test_g3_watchdog.py

# Local report-only watchdog inventory (never deletes)
uv run python scripts/g3/watchdog.py --subscription "$G3_SUBSCRIPTION_ID" --armed-session-id s20261001-1200-ab12cd --armed-expiry-utc 2026-10-01T15:50:00Z
```

G4-only commands (all human-approved; none run in G3): `terraform apply`,
`az role assignment create` (performed by Terraform apply), starting the
runbook, a quota request, `session.py reserve`, `session.py bind`,
`deploy_session.sh --i-understand` with `G3_ALLOW_CLOUD_MUTATION=1`.

## G4 checklist: live watchdog verification before any GPU provisioning

1. Fresh `discover.py` with **no blockers**: the T4 family quota must be
   raised by a human request (≥ 4 vCPU) and re-verified; capacity is still
   only proven by node-pool creation.
2. Fresh `prices.py` evidence (≤ 72 h); re-run `estimate` and `admit`.
3. Apply the controls root (`armed_session = null`, Report mode); confirm the
   three groups, the identity's role assignments and the result container.
4. Start `Invoke-SessionExpiry` manually (Report): job **Completed**, result
   JSON in `watchdog-results`, outcome `already-clean` or `report-only`.
5. Arm a throw-away session id + a tagged, free test resource in the session
   group; switch to Execute; confirm the one-time job deletes it, polls, and
   re-inventories; confirm the hourly sweep then reports `already-clean`.
6. Prove the failure path: a resource tagged with a different session id is
   left in place and the job reports `unresolved` (throws).
7. `session.py watchdog-verified --result <blob> --job-id <job>`; confirm
   `admit` no longer lists "watchdog not live-verified".
8. Upload immutable inputs to `inputs/`; confirm the kubelet read role after
   the first session apply.
9. `session.py reserve` → arm the **real** session (controls apply) →
   `bind` → `verify-binding` → session apply with the GPU pool **disabled**;
   confirm port-forward access with Entra login and that no public ingress
   exists.
10. Build and push the **linux/amd64 CUDA** training image (separate
    lockfile); only then a second bound plan with `gpu_node_pool_enabled`.
11. After the session: watchdog result → `session.py teardown`; Cost
    Management query (with the GBP→USD invoice rate) → `session.py reconcile`;
    the reservation stays until coverage is complete and teardown verified.

## Confirmation

G3 created, modified and deleted **no Azure resources**. All Azure calls were
read-only (`az account/provider/vm/aks/group/role/ad/rest GET+POST query`),
plus Terraform plans that were never applied. Cloud cost of G3: **USD 0**.
Security: no credentials, tokens, state files, binary plans or private
account evidence are committed; runtime evidence lives under the git-ignored
`.local/g3/`. Cost implications of the design: a full T4 session reserves
about USD 6.52; the control resources cost pennies per month; the retained
storage costs cents per month and expires by policy.
