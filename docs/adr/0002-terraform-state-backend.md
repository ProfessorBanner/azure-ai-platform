# ADR 0002: Store Terraform state in Azure Blob Storage

## Status

Accepted

## Context

Local Terraform state is unsuitable for collaboration, CI/CD, state locking
and controlled recovery. State may contain sensitive infrastructure data and
must not be committed to Git.

## Decision

Store Terraform state in a dedicated Azure Storage account. The bootstrap
configuration initially uses local state and then migrates itself to the Azure
Storage backend.

The target hardening posture and its current implementation status are tracked
below. This distinction matters: earlier revisions of this ADR described the
full target as though already built, which did not match the deployed account.

### Currently in effect

- A dedicated bootstrap resource group.
- A private blob container.
- Microsoft Entra ID authentication for the backend
  (`use_azuread_auth = true`; no Shared Key used by the backend).
- Container-scoped Blob Data Contributor permissions.
- TLS 1.2 minimum and HTTPS-only traffic.
- Separate state keys for platform components and environments.
- An Azure CanNotDelete resource lock on the storage account, created
  out-of-band (present in Azure, not yet managed by Terraform — see
  "Deferred").

### Introduced by this change (code only — not yet applied)

- Blob versioning.
- Blob soft deletion (30-day retention).
- Container soft deletion (30-day retention).
- Terraform `prevent_destroy` on the storage account.
- Removal of the unused `tenant_id` variable.

These are safe in-place updates. They require a reviewed `terraform apply`;
no apply has been performed.

### Gated to a separate reviewed apply (follow-up — not in current code)

- Disabling Shared Key authentication (`shared_access_key_enabled = false`).
  Deliberately not flipped in this change; the code still sets `true`. Adopting
  it is a follow-up: set the value to `false`, apply on its own, then run
  `terraform init -reconfigure` and `terraform plan` to confirm the backend
  still authenticates via Entra ID before any further work depends on it.

### Deferred (not addressed here)

- Bringing the CanNotDelete lock under Terraform management (future
  `terraform import` — see the recovery runbook).
- Restricting public network access / setting the network default action to
  `Deny` (requires known CI and workstation egress, or a private endpoint,
  to avoid loss of state access).
- Private endpoints.
- Customer-managed keys and infrastructure encryption. Both require account
  replacement and are therefore out of scope for the existing state account;
  accepted as deviations for now.

## State naming convention

The bootstrap state currently uses the key `bootstrap.tfstate` (see
`backend/dev.hcl`). This is the deployed key and is not renamed here, since
changing it forces a state migration.

The intended forward convention for components and environments is:

- `bootstrap/dev.tfstate`
- `platform/dev.tfstate`
- `databricks/dev.tfstate`
- `applications/<application-name>/dev.tfstate`

Adopting this convention for the bootstrap key is deferred and would be a
deliberate, separately reviewed state migration.

Production state will use separate keys and may later use a separate storage
account or subscription.

## Consequences

### Positive

- Native state locking.
- Entra-based access control.
- State history and deletion recovery.
- Suitable foundation for CI/CD.
- No long-lived storage-account keys.

### Negative

- Bootstrap requires a two-phase process.
- RBAC propagation can temporarily cause 403 errors.
- State storage becomes a critical platform dependency.
- Recovery and access procedures must be maintained.


Finding: AZU-0012
Reason: Azure DevOps Microsoft-hosted agents use non-static outbound IP ranges,
and restricting the account now could block Terraform backend access.
Compensating controls:
- Entra workload identity federation
- container-scoped Storage Blob Data Contributor
- private state container
- TLS 1.2 and HTTPS-only
- versioning and soft-delete
- prevent_destroy
Expiry/review date: 2026-11-30
Remediation options:
- self-hosted agent with stable egress
- selected network rules
- private endpoint and private DNS
