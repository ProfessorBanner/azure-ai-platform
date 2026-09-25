# ADR 0012: AKS MLOps lab cost controls and expiry watchdog

## Status

Accepted (G3, 2026-09-17). No Azure resources exist yet.

## Context

The Pet Image Classifier (`products/pet-classifier`) will demonstrate GPU
training and MLOps on AKS with a **USD 100 lifetime** cloud budget. G1 and G2
proved the product locally at USD 0. Before any GPU compute is provisioned
the project needs: a reproducible way to price a session from public
evidence, a lifetime ledger that never resets, an admission rule that keeps
planned exposure at or under USD 60 with USD 40 of untouchable contingency,
and an expiry mechanism that survives a closed laptop and a broken cluster.

Discovery (2026-09-17, UK South) established: all required providers are
registered; `Standard_NC4as_T4_v3` is listed without restrictions but its
family quota is **0**; the Dasv5/Dsv5 families are also at 0 while Dalv6
has 10 vCPU, making `Standard_D4als_v6` the viable 4 vCPU system node; AKS
1.35 is the default line; the subscription bills in **GBP** while retail
prices are USD; Azure Automation supports one-time and hourly schedules with
PowerShell 7.4 and stops jobs after three hours of fair share.

## Decision

1. **Three lifecycle classes in three resource groups**: control (watchdog
   and its identity), retained (inputs, models, watchdog results; blobs
   expire by lifecycle rule) and disposable (AKS, transport registry, and the
   AKS-managed node group). The disposable group is created **empty by the
   controls root** so the watchdog's role assignment can be scoped to it
   before compute exists.
2. **Azure Automation is the expiry watchdog**: a PowerShell 7.4 runbook run
   by a system-assigned managed identity on a per-session one-time schedule
   and an independent hourly sweep. Report-only by default; Execute only
   after live verification. Eligibility requires exact subscription, exact
   group id, matching project/session/expiry/lifecycle tags and reached
   expiry; deletion is idempotent, polled, re-inventoried and reported as
   unresolved on any residue. It is a bounded control, not a billing cap.
3. **Least privilege for the watchdog**: a custom delete-only role scoped to
   the session group, Reader at subscription scope for re-inventory of the
   AKS node group, and blob write on one container. No Contributor, no
   wildcard scopes, no credentials.
4. **Two Terraform roots and states** (`capabilities/aks-mlops/controls`,
   `capabilities/aks-mlops/session`), pinned to `azurerm ~> 4.0` with
   `resource_provider_registrations = "none"`, independent of every
   platform, Databricks and Foundry state.
5. **Cost controls in the product**: a versioned JSON policy (100/60/40, one
   session, one GPU node, 120-minute target, 1800-second Job deadline,
   explicit allowances and bounds), retail price evidence with exact-match
   selection and hashes, Decimal estimates rounded up with explicit FX and
   tax allowances, a lifetime ledger with idempotent reservations and
   reconciliation, and an admission decision that reports "cost fits" and
   "cloud execution allowed" separately with no bypass.
6. **Session lab shape**: AKS Free tier, one x86-64 system node, at most one
   T4 user node (disabled initially, fixed count, no autoscaling, never
   Spot), Standard LB egress priced, Basic ACR per session, port-forward
   only, Entra-only access.
7. **G4 boundary**: quota request, runbook live verification, arming,
   applies, the linux/amd64 CUDA training image and any GPU provisioning.

## Consequences

- Spending is governed by reservation, ledger and expiry, not by a hard
  technical cap; planned exposure targets USD 60 and USD 40 stays in reserve.
- The G2 CPU-only arm64 image cannot train on the T4 node; G4 needs a
  separate amd64 CUDA image and lockfile.
- One system node is a documented deviation from the production
  recommendation; the count variable allows two if AKS refuses one.
- Reconciliation must convert GBP invoices explicitly; until coverage is
  complete and teardown verified, the reservation is retained.
- The watchdog runbook is reviewed but unverified until G4 stage 2; real
  compute admission is blocked until that evidence is recorded.

## References

- `products/pet-classifier/docs/g3-cost-controls.md`
- `infrastructure/capabilities/aks-mlops/{controls,session}`
- ADR 0006 (capability roots outside the platform environments)
